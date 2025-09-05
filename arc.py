#!/usr/bin/env python3

import os
import sys
import argparse
import subprocess
import urllib.request
import tempfile
import time
import json
import re
import fnmatch
import ssl
import urllib3
import contextlib
import io
from kubernetes import client, config
from kubernetes.stream import stream

try:
    from retis import EventFile
    RETIS_ANALYSIS_AVAILABLE = True
except ImportError:
    RETIS_ANALYSIS_AVAILABLE = False


class KubernetesDebugPodManager:
    """
    A modern, Kubernetes-native replacement for 'oc debug node' commands.
    
    This class provides a clean, modular interface for creating debug pods,
    executing commands, and managing file operations on Kubernetes nodes.
    """
    
    def __init__(self, k8s_client: client.CoreV1Api, namespace: str = "default"):
        """
        Initialize the debug pod manager.
        
        Args:
            k8s_client: Kubernetes CoreV1Api client instance
            namespace: Namespace to create debug pods in
        """
        self.k8s_client = k8s_client
        self.namespace = namespace
        self.active_pods = {}  # Track active debug pods by node
    
    def create_debug_pod(self, node_name: str, image: str = "registry.redhat.io/ubi8/ubi:latest", 
                        timeout: int = 60) -> str:
        """
        Create a debug pod on the specified node with privileged access.
        
        Args:
            node_name: Target node name
            image: Container image to use for the debug pod
            timeout: Timeout in seconds for pod to become ready
        
        Returns:
            str: Name of the created debug pod
            
        Raises:
            Exception: If pod creation fails or times out
        """
        pod_name = f"debug-{node_name.replace('.', '-')}-{int(time.time())}"
        
        # Define the container with proper security context and volume mounts
        container = client.V1Container(
            name="debug-container",
            image=image,
            # Keep the pod running so we can exec into it
            command=["/bin/sh", "-c", "sleep 1d"],
            security_context=client.V1SecurityContext(
                privileged=True,  # Required for chroot operations
                capabilities=client.V1Capabilities(
                    add=["SYS_ADMIN", "NET_ADMIN", "SYS_PTRACE"]
                )
            ),
            volume_mounts=[
                # Mount host's root filesystem to allow chrooting
                client.V1VolumeMount(name="host-root", mount_path="/host"),
            ],
        )

        # Define volumes from the host
        volumes = [
            client.V1Volume(
                name="host-root",
                host_path=client.V1HostPathVolumeSource(path="/"),
            ),
        ]

        # Add tolerations to allow running on any node (including control-plane)
        tolerations = [
            client.V1Toleration(operator="Exists")
        ]

        # Define the Pod Spec
        pod_spec = client.V1PodSpec(
            containers=[container],
            host_pid=True,  # Access the host's PID namespace
            host_network=True,  # Access the host's network namespace
            node_name=node_name,
            volumes=volumes,
            tolerations=tolerations,
            restart_policy="Never"  # Do not restart the pod automatically
        )
        
        # Create the Pod object
        pod = client.V1Pod(
            api_version="v1",
            kind="Pod",
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=self.namespace,
                labels={
                    "app": "debug-pod",
                    "node": node_name.replace('.', '-'),
                    "created-by": "arc-retis-collection"
                }
            ),
            spec=pod_spec
        )
        
        try:
            # Create the pod
            print(f"Creating debug pod '{pod_name}' on node '{node_name}'...")
            self.k8s_client.create_namespaced_pod(namespace=self.namespace, body=pod)
            
            # Wait for pod to be ready
            print(f"Waiting for debug pod to become ready...")
            start_time = time.time()
            while time.time() - start_time < timeout:
                try:
                    pod_status = self.k8s_client.read_namespaced_pod_status(
                        name=pod_name, namespace=self.namespace
                    )
                    
                    if pod_status.status.phase == "Running":
                        # Check if container is ready
                        if (pod_status.status.container_statuses and 
                            pod_status.status.container_statuses[0].ready):
                            print(f"✓ Debug pod '{pod_name}' is ready")
                            self.active_pods[node_name] = pod_name
                            return pod_name
                    elif pod_status.status.phase in ["Failed", "Succeeded"]:
                        raise Exception(f"Pod '{pod_name}' ended unexpectedly in phase: {pod_status.status.phase}")
                        
                except client.ApiException as e:
                    if e.status != 404:  # Pod might not exist yet
                        raise
                
                time.sleep(2)
            
            raise Exception(f"Timeout waiting for debug pod '{pod_name}' to become ready")
            
        except Exception as e:
            # Clean up on failure
            try:
                self.delete_debug_pod(node_name, pod_name)
            except:
                pass
            raise Exception(f"Failed to create debug pod on node '{node_name}': {e}")
    
    def execute_command(self, node_name: str, command: str, use_chroot: bool = True, 
                       timeout: int = 300) -> tuple[bool, str, str]:
        """
        Execute a command in the debug pod.
        
        Args:
            node_name: Target node name
            command: Command to execute
            use_chroot: Whether to use chroot /host (default: True)
            timeout: Command timeout in seconds
        
        Returns:
            tuple: (success: bool, stdout: str, stderr: str)
        """
        pod_name = self.active_pods.get(node_name)
        if not pod_name:
            raise Exception(f"No active debug pod found for node '{node_name}'")
        
        # Prepare the command
        if use_chroot:
            exec_command = ["chroot", "/host", "sh", "-c", command]
        else:
            exec_command = ["sh", "-c", command]
        
        print(f"Executing command in debug pod '{pod_name}': {command}")
        
        try:
            # Execute the command using Kubernetes stream API
            resp = stream(
                self.k8s_client.connect_get_namespaced_pod_exec,
                pod_name,
                self.namespace,
                command=exec_command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _request_timeout=timeout
            )
            
            # For the stream API, we need to capture both stdout and stderr
            # The response contains both stdout and stderr mixed
            stdout = resp
            stderr = ""
            
            return True, stdout, stderr
            
        except Exception as e:
            error_msg = f"Command execution failed: {e}"
            print(f"✗ {error_msg}")
            return False, "", error_msg
    
    def copy_file_to_pod(self, node_name: str, local_path: str, remote_path: str, 
                        use_host_path: bool = True) -> bool:
        """
        Copy a file from local machine to the debug pod.
        
        Args:
            node_name: Target node name
            local_path: Local file path
            remote_path: Remote file path (on host if use_host_path=True)
            use_host_path: Whether to use /host prefix for remote path
        
        Returns:
            bool: Success status
        """
        pod_name = self.active_pods.get(node_name)
        if not pod_name:
            raise Exception(f"No active debug pod found for node '{node_name}'")
        
        try:
            # Read the local file
            with open(local_path, 'rb') as f:
                file_content = f.read()
            
            # Determine the target path in the pod
            if use_host_path:
                target_path = f"/host{remote_path}"
            else:
                target_path = remote_path
            
            # Create the directory if it doesn't exist
            dir_path = os.path.dirname(target_path)
            if dir_path:
                mkdir_cmd = f"mkdir -p {dir_path}"
                success, _, _ = self.execute_command(node_name, mkdir_cmd, use_chroot=False)
                if not success:
                    print(f"⚠ Warning: Failed to create directory {dir_path}")
            
            # Use base64 encoding to transfer the file
            import base64
            encoded_content = base64.b64encode(file_content).decode('utf-8')
            
            # Write the file using echo and base64 decode
            write_cmd = f"echo '{encoded_content}' | base64 -d > {target_path}"
            success, stdout, stderr = self.execute_command(node_name, write_cmd, use_chroot=False)
            
            if success:
                print(f"✓ File copied to {target_path}")
                return True
            else:
                print(f"✗ Failed to copy file: {stderr}")
                return False
                
        except Exception as e:
            print(f"✗ Error copying file to pod: {e}")
            return False
    
    def copy_file_from_pod(self, node_name: str, remote_path: str, local_path: str, 
                          use_host_path: bool = True) -> bool:
        """
        Copy a file from the debug pod to local machine.
        
        Args:
            node_name: Target node name
            remote_path: Remote file path (on host if use_host_path=True)
            local_path: Local file path
            use_host_path: Whether to use /host prefix for remote path
        
        Returns:
            bool: Success status
        """
        pod_name = self.active_pods.get(node_name)
        if not pod_name:
            raise Exception(f"No active debug pod found for node '{node_name}'")
        
        try:
            # Determine the source path in the pod
            if use_host_path:
                source_path = f"/host{remote_path}"
            else:
                source_path = remote_path
            
            # Read the file using base64 encoding
            read_cmd = f"base64 {source_path}"
            success, stdout, stderr = self.execute_command(node_name, read_cmd, use_chroot=False)
            
            if not success:
                print(f"✗ Failed to read file from pod: {stderr}")
                return False
            
            # Decode the base64 content
            import base64
            try:
                file_content = base64.b64decode(stdout.strip())
            except Exception as e:
                print(f"✗ Failed to decode file content: {e}")
                return False
            
            # Create local directory if needed
            local_dir = os.path.dirname(local_path)
            if local_dir:
                os.makedirs(local_dir, exist_ok=True)
            
            # Write the file locally
            with open(local_path, 'wb') as f:
                f.write(file_content)
            
            print(f"✓ File copied to {local_path}")
            return True
            
        except Exception as e:
            print(f"✗ Error copying file from pod: {e}")
            return False
    
    def delete_debug_pod(self, node_name: str, pod_name: str = None) -> bool:
        """
        Delete a debug pod.
        
        Args:
            node_name: Node name (used to look up active pod if pod_name not provided)
            pod_name: Specific pod name to delete (optional)
        
        Returns:
            bool: Success status
        """
        if not pod_name:
            pod_name = self.active_pods.get(node_name)
            if not pod_name:
                print(f"No active debug pod found for node '{node_name}'")
                return True
        
        try:
            print(f"Deleting debug pod '{pod_name}'...")
            self.k8s_client.delete_namespaced_pod(
                name=pod_name,
                namespace=self.namespace,
                grace_period_seconds=0  # Force immediate deletion
            )
            
            # Remove from active pods tracking
            if node_name in self.active_pods and self.active_pods[node_name] == pod_name:
                del self.active_pods[node_name]
            
            print(f"✓ Debug pod '{pod_name}' deleted")
            return True
            
        except client.ApiException as e:
            if e.status == 404:
                print(f"Debug pod '{pod_name}' not found (may have been already deleted)")
                return True
            else:
                print(f"✗ Failed to delete debug pod '{pod_name}': {e}")
                return False
        except Exception as e:
            print(f"✗ Error deleting debug pod '{pod_name}': {e}")
            return False
    
    def cleanup_all_pods(self) -> None:
        """Clean up all active debug pods."""
        print("Cleaning up all debug pods...")
        for node_name, pod_name in list(self.active_pods.items()):
            self.delete_debug_pod(node_name, pod_name)
    
    @contextlib.contextmanager
    def debug_pod_context(self, node_name: str, image: str = "registry.redhat.io/ubi8/ubi:latest"):
        """
        Context manager for debug pod lifecycle.
        
        Usage:
            with debug_manager.debug_pod_context("worker-1") as pod_name:
                success, stdout, stderr = debug_manager.execute_command("worker-1", "ls /")
        """
        pod_name = None
        try:
            pod_name = self.create_debug_pod(node_name, image)
            yield pod_name
        finally:
            if pod_name:
                self.delete_debug_pod(node_name, pod_name)

def get_kubeconfig_path(args):
    """Get the kubeconfig path from arguments or user input."""
    if args.kubeconfig:
        kubeconfig_path = args.kubeconfig
        print(f"Using kubeconfig from command line argument: {kubeconfig_path}")
    else:
        print("No kubeconfig path provided via --kubeconfig argument.")
        kubeconfig_path = input("Please enter the path to your kubeconfig file: ").strip()
        if not kubeconfig_path:
            print("No kubeconfig path provided. Exiting.")
            sys.exit(1)
    
    # Expand user home directory if needed
    kubeconfig_path = os.path.expanduser(kubeconfig_path)
    
    # Check if file exists
    if not os.path.exists(kubeconfig_path):
        print(f"Kubeconfig file not found at: {kubeconfig_path}")
        print("Please verify the file path exists.")
        sys.exit(1)
    
    return kubeconfig_path

def get_nodes_from_kubernetes(api_instance, name_filter=None, workload_filter=None):
    """Get nodes using Kubernetes client API with optional filtering."""
    print("Getting nodes using Kubernetes API...")
    
    try:
        # Get all nodes using the Kubernetes API
        nodes = api_instance.list_node()
        all_nodes = []
        
        for node in nodes.items:
            node_name = node.metadata.name
            
            # Check if node is worker (not master/control-plane)
            node_labels = node.metadata.labels or {}
            is_worker = (
                'node-role.kubernetes.io/worker' in node_labels or
                ('node-role.kubernetes.io/master' not in node_labels and
                 'node-role.kubernetes.io/control-plane' not in node_labels)
            )
            
            if is_worker:
                all_nodes.append(node_name)
        
        print(f"✓ Found {len(all_nodes)} worker nodes")
        
        # Show actual node names for debugging
        if all_nodes:
            print("Available worker nodes:")
            for i, node in enumerate(all_nodes, 1):
                print(f"  {i}. {node}")
        
        # Apply name filtering if specified
        filtered_nodes = all_nodes
        if name_filter:
            print(f"Applying name filter: '{name_filter}'")
            
            # Try multiple matching strategies for better user experience
            matched_nodes = []
            
            # Strategy 1: Exact match (case-insensitive)
            exact_matches = [node for node in filtered_nodes if node.lower() == name_filter.lower()]
            if exact_matches:
                matched_nodes = exact_matches
                print(f"  Using exact match strategy")
            
            # Strategy 2: Substring match (case-insensitive)
            elif not exact_matches:
                substring_matches = [node for node in filtered_nodes if name_filter.lower() in node.lower()]
                if substring_matches:
                    matched_nodes = substring_matches
                    print(f"  Using substring match strategy")
            
            # Strategy 3: Glob pattern matching (supports * and ? wildcards)
            if not matched_nodes:
                glob_matches = [node for node in filtered_nodes if fnmatch.fnmatch(node.lower(), name_filter.lower())]
                if glob_matches:
                    matched_nodes = glob_matches
                    print(f"  Using glob pattern match strategy")
            
            # Strategy 4: If filter contains wildcards but no matches, suggest adding wildcards
            if not matched_nodes and not any(wildcard in name_filter for wildcard in ['*', '?']):
                # Try with wildcards automatically
                wildcard_pattern = f"*{name_filter.lower()}*"
                wildcard_matches = [node for node in filtered_nodes if fnmatch.fnmatch(node.lower(), wildcard_pattern)]
                if wildcard_matches:
                    matched_nodes = wildcard_matches
                    print(f"  Using automatic wildcard pattern: '{wildcard_pattern}'")
            
            filtered_nodes = matched_nodes
            print(f"✓ After name filter '{name_filter}': {len(filtered_nodes)} nodes")
            
            # Show which nodes matched for debugging
            if filtered_nodes:
                print("Matched nodes:")
                for i, node in enumerate(filtered_nodes, 1):
                    print(f"  {i}. {node}")
            else:
                print("No nodes matched the filter. Try using:")
                print(f"  - Partial name: part of the node name")
                print(f"  - Wildcard pattern: '*{name_filter}*' or '{name_filter}*'")
                print(f"  - Available nodes are listed above")
        
        # Apply workload filtering if specified
        if workload_filter and filtered_nodes:
            workload_filtered_nodes = []
            for node in filtered_nodes:
                if has_workload_on_node(api_instance, node, workload_filter):
                    workload_filtered_nodes.append(node)
            filtered_nodes = workload_filtered_nodes
            print(f"✓ After workload filter '{workload_filter}': {len(filtered_nodes)} nodes")
        
        if not filtered_nodes:
            print("✗ No nodes match the specified filters")
            return []
        
        print(f"✓ Selected nodes for RETIS collection:")
        for node in filtered_nodes:
            print(f"  - {node}")
        
        return filtered_nodes
        
    except client.ApiException as e:
        print(f"✗ Kubernetes API error getting nodes: {e}")
        return []
    except Exception as e:
        print(f"✗ Error getting nodes: {e}")
        return []


def has_workload_on_node(api_instance, node_name, workload_filter):
    """Check if a specific workload is running on the given node using Kubernetes API."""
    try:
        # Get pods running on the specific node using field selector
        pods = api_instance.list_pod_for_all_namespaces(
            field_selector=f'spec.nodeName={node_name}'
        )
        
        for pod in pods.items:
            pod_name = pod.metadata.name
            namespace = pod.metadata.namespace
            
            # Check if the workload filter matches pod name, namespace, or labels
            if re.search(workload_filter, pod_name, re.IGNORECASE):
                return True
            if re.search(workload_filter, namespace, re.IGNORECASE):
                return True
            
            # Check labels
            labels = pod.metadata.labels or {}
            for key, value in labels.items():
                if re.search(workload_filter, f"{key}={value}", re.IGNORECASE):
                    return True
        
        return False
        
    except client.ApiException as e:
        print(f"⚠ Warning: Kubernetes API error checking workloads on node {node_name}: {e}")
        return False
    except Exception as e:
        print(f"⚠ Warning: Error checking workloads on node {node_name}: {e}")
        return False



def download_retis_script_locally(script_url="https://raw.githubusercontent.com/retis-org/retis/main/tools/retis_in_container.sh"):
    """Download the retis_in_container.sh script locally."""
    print(f"Downloading retis_in_container.sh from {script_url}...")
    
    try:
        # Create a temporary file
        temp_fd, temp_path = tempfile.mkstemp(suffix='.sh', prefix='retis_in_container_')
        
        with os.fdopen(temp_fd, 'wb') as temp_file:
            with urllib.request.urlopen(script_url) as response:
                temp_file.write(response.read())
        
        # Make the local file executable
        os.chmod(temp_path, 0o755)
        
        print(f"✓ Downloaded retis_in_container.sh to {temp_path}")
        return temp_path
        
    except Exception as e:
        print(f"✗ Failed to download retis_in_container.sh: {e}")
        return None

def setup_script_on_node(node_name, working_directory, local_script_path, 
                         debug_manager: KubernetesDebugPodManager = None, dry_run=False):
    """Copy the retis_in_container.sh script to a specific node and set permissions using Kubernetes-native debug pod."""
    print(f"Checking retis_in_container.sh on node {node_name}...")
    
    if dry_run:
        print(f"[DRY RUN] Would check if {working_directory}/retis_in_container.sh exists with correct permissions")
        print(f"[DRY RUN] Would create working directory {working_directory} on {node_name} if needed")
        print(f"[DRY RUN] Would copy {local_script_path} to {node_name}:{working_directory}/retis_in_container.sh if needed")
        print(f"[DRY RUN] Would set executable permissions on the script if needed")
        return True
    
    if not debug_manager:
        raise Exception("debug_manager is required for Kubernetes-native operations")
    
    try:
        # Create debug pod for all operations
        with debug_manager.debug_pod_context(node_name) as pod_name:
            script_path = f"{working_directory}/retis_in_container.sh"
            
            # First, check if the script already exists with correct permissions
            print(f"Checking existing script on {node_name}...")
            check_command = f"ls -la {script_path}"
            success, stdout, stderr = debug_manager.execute_command(
                node_name, check_command, use_chroot=True, timeout=30
            )
            
            script_exists = False
            script_executable = False
            
            if success and stdout:
                script_exists = True
                # Check if the script is executable (look for 'x' in permissions)
                permissions = stdout.strip()
                print(f"Found existing script: {permissions}")
                
                # Check if user, group, or other has execute permission
                if 'x' in permissions[:10]:  # First 10 characters contain permissions
                    script_executable = True
                    print(f"✓ Script already exists with correct permissions on {node_name}")
                    return True
                else:
                    print(f"⚠ Script exists but is not executable on {node_name}")
            else:
                print(f"Script does not exist on {node_name}")
            
            # Create working directory if needed
            print(f"Ensuring directory exists on {node_name}...")
            mkdir_command = f"mkdir -p {working_directory}"
            mkdir_success, _, mkdir_stderr = debug_manager.execute_command(
                node_name, mkdir_command, use_chroot=True, timeout=30
            )
            
            if not mkdir_success:
                print(f"⚠ Warning: Failed to create directory on {node_name}: {mkdir_stderr}")
            
            # Only copy script if it doesn't exist
            if not script_exists:
                print(f"Copying script to {node_name}...")
                
                # Copy the script to the node using our debug pod manager
                copy_success = debug_manager.copy_file_to_pod(
                    node_name, local_script_path, script_path, use_host_path=True
                )
                
                if not copy_success:
                    print(f"✗ Failed to copy script to {node_name}")
                    return False
                
                print(f"✓ Script copied to {node_name}")
            
            # Set executable permissions if script is not executable
            if not script_executable:
                print(f"Setting executable permissions on {node_name}...")
                chmod_command = f"chmod a+x {script_path}"
                chmod_success, _, chmod_stderr = debug_manager.execute_command(
                    node_name, chmod_command, use_chroot=True, timeout=30
                )
                
                if not chmod_success:
                    print(f"✗ Failed to set permissions on {node_name}: {chmod_stderr}")
                    return False
                
                print(f"✓ Executable permissions set on {node_name}")
            
            # Final verification
            print(f"Verifying script setup on {node_name}...")
            verify_success, verify_stdout, verify_stderr = debug_manager.execute_command(
                node_name, f"ls -la {script_path}", use_chroot=True, timeout=30
            )
            
            if verify_success:
                print(f"✓ Script setup complete on {node_name}")
                if verify_stdout:
                    print(f"Final file info: {verify_stdout.strip()}")
                return True
            else:
                print(f"✗ Script verification failed on {node_name}: {verify_stderr}")
                return False
        
    except Exception as e:
        print(f"✗ Error setting up script on {node_name}: {e}")
        return False

def stop_retis_on_node(node_name, debug_manager: KubernetesDebugPodManager, dry_run=False):
    """Stop the RETIS systemd unit on a specific node using Kubernetes-native debug pod."""
    print(f"Stopping RETIS collection on node: {node_name}")
    
    if dry_run:
        print(f"[DRY RUN] Would execute command:")
        print(f"  Create debug pod on {node_name}")
        print(f"  Execute: systemctl stop RETIS")
        return True
    
    try:
        # Create debug pod and execute stop command
        with debug_manager.debug_pod_context(node_name) as pod_name:
            print(f"Executing stop command via debug pod...")
            
            # Stop the RETIS systemd unit
            stop_command = "systemctl stop RETIS"
            success, stdout, stderr = debug_manager.execute_command(
                node_name, stop_command, use_chroot=True, timeout=60
            )
            
            if not success:
                print(f"✗ RETIS stop command failed on {node_name}")
                if stderr:
                    print("Stop error output:")
                    print(stderr)
                return False
            else:
                print(f"✓ RETIS systemd unit successfully stopped on {node_name}")
                if stdout.strip():
                    print("Stop output:")
                    print(stdout)
                return True
        
    except Exception as e:
        print(f"✗ Error stopping RETIS on {node_name}: {e}")
        return False

def reset_failed_retis_on_node(node_name, debug_manager: KubernetesDebugPodManager, dry_run=False):
    """Reset failed RETIS systemd unit on a specific node using Kubernetes-native debug pod."""
    print(f"Resetting failed RETIS unit on node: {node_name}")
    
    if dry_run:
        print(f"[DRY RUN] Would execute command:")
        print(f"  Create debug pod on {node_name}")
        print(f"  Execute: systemctl reset-failed")
        return True
    
    try:
        # Create debug pod and execute reset-failed command
        with debug_manager.debug_pod_context(node_name) as pod_name:
            print(f"Executing reset-failed command via debug pod...")
            
            # Reset failed RETIS systemd unit
            reset_command = "systemctl reset-failed"
            success, stdout, stderr = debug_manager.execute_command(
                node_name, reset_command, use_chroot=True, timeout=60
            )
            
            if not success:
                print(f"✗ RETIS reset-failed command failed on {node_name}")
                if stderr:
                    print("Reset-failed error output:")
                    print(stderr)
                return False
            else:
                print(f"✓ RETIS systemd unit successfully reset on {node_name}")
                if stdout.strip():
                    print("Reset-failed output:")
                    print(stdout)
                return True
        
    except Exception as e:
        print(f"✗ Error resetting failed RETIS on {node_name}: {e}")
        return False

def download_results_from_node(node_name, working_directory, output_file, local_download_dir="./", 
                              debug_manager: KubernetesDebugPodManager = None, dry_run=False):
    """Download RETIS results file from a specific node to local machine using Kubernetes-native debug pod."""
    node_short_name = node_name.split('.')[0]  # Get short name for file naming
    local_filename = f"arc_{node_short_name}_{output_file}"
    local_filepath = os.path.join(local_download_dir, local_filename)
    remote_filepath = f"{working_directory}/{output_file}"
    
    print(f"Downloading RETIS results from node: {node_name}")
    print(f"Remote file: {remote_filepath}")
    print(f"Local file: {local_filepath}")
    
    if dry_run:
        print(f"[DRY RUN] Would execute commands:")
        print(f"  1. Create debug pod on {node_name}")
        print(f"  2. Check if file exists: {remote_filepath}")
        print(f"  3. Copy file to: {local_filepath}")
        return True
    
    if not debug_manager:
        raise Exception("debug_manager is required for Kubernetes-native operations")
    
    try:
        # Create debug pod and download file
        with debug_manager.debug_pod_context(node_name) as pod_name:
            print(f"Checking if results file exists on {node_name}...")
            
            # Check if remote file exists first
            check_command = f"ls -la {remote_filepath}"
            success, stdout, stderr = debug_manager.execute_command(
                node_name, check_command, use_chroot=True, timeout=30
            )
            
            if not success:
                print(f"⚠ Results file {remote_filepath} not found on {node_name}")
                if stderr:
                    print(f"Check error: {stderr}")
                return False
            
            print(f"✓ Results file found on {node_name}")
            if stdout.strip():
                print(f"File info: {stdout.strip()}")
            
            # Create local download directory if it doesn't exist
            os.makedirs(local_download_dir, exist_ok=True)
            
            # Copy the results file from the node to local machine
            print(f"Downloading file from {node_name}...")
            copy_success = debug_manager.copy_file_from_pod(
                node_name, remote_filepath, local_filepath, use_host_path=True
            )
            
            if not copy_success:
                print(f"✗ Failed to download results from {node_name}")
                return False
            
            # Verify the file was downloaded
            if os.path.exists(local_filepath):
                file_size = os.path.getsize(local_filepath)
                print(f"✓ Results successfully downloaded from {node_name}")
                print(f"Local file: {local_filepath} ({file_size} bytes)")
                return True
            else:
                print(f"✗ Download failed - local file not found: {local_filepath}")
                return False
        
    except Exception as e:
        print(f"✗ Error downloading results from {node_name}: {e}")
        return False

def run_retis_on_node(node_name, retis_image, working_directory, retis_args=None, retis_cmd_str=None, 
                     debug_manager: KubernetesDebugPodManager = None, dry_run=False):
    """Run RETIS collection on a specific node using Kubernetes-native debug pod."""
    
    # Set default retis_args if not provided (for backwards compatibility)
    if retis_args is None:
        retis_args = {
            'output_file': 'events.json',
            'allow_system_changes': True,
            'ovs_track': True,
            'stack': True,
            'probe_stack': True,
            'filter_packet': 'tcp port 8080 or tcp port 8081',
            'retis_extra_args': '',
            'retis_tag': 'v1.5.2'
        }
    
    # Use custom command string if provided, otherwise build from retis_args
    if retis_cmd_str is None:
        # Build the retis collect command arguments
        retis_cmd_args = ['collect']
        
        # Add output file
        retis_cmd_args.extend(['-o', retis_args['output_file']])
        
        # Add boolean flags
        if retis_args['allow_system_changes']:
            retis_cmd_args.append('--allow-system-changes')
        
        if retis_args['ovs_track']:
            retis_cmd_args.append('--ovs-track')
        
        if retis_args['stack']:
            retis_cmd_args.append('--stack')
        
        if retis_args['probe_stack']:
            retis_cmd_args.append('--probe-stack')
        
        # Add packet filter
        if retis_args['filter_packet']:
            retis_cmd_args.extend(['--filter-packet', f"'{retis_args['filter_packet']}'"])
        
        # Add any extra arguments
        if retis_args['retis_extra_args']:
            retis_cmd_args.extend(retis_args['retis_extra_args'].split())
        
        # Join all arguments
        retis_cmd_str = ' '.join(retis_cmd_args)
        print(f"Built RETIS command from parameters")
    else:
        print(f"Using custom RETIS command string")
    
    # Construct the shell command that will be executed after 'sh -c'
    # Use full path to the script since we downloaded it to the working directory
    shell_command = f"export RETIS_TAG={retis_args['retis_tag']}; export RETIS_IMAGE='{retis_image}'; {working_directory}/retis_in_container.sh {retis_cmd_str}"
    
    # Construct the systemd-run command
    systemd_command = f'systemd-run --unit="RETIS" --working-directory={working_directory} sh -c "{shell_command}"'
    
    print(f"\nRunning RETIS collection on node: {node_name}")
    print(f"Working directory: {working_directory}")
    print(f"RETIS Image: {retis_image}")
    print(f"RETIS Tag: {retis_args['retis_tag']}")
    print(f"RETIS Arguments: {retis_cmd_str}")
    print(f"DEBUG: Shell command: {shell_command}")
    print(f"DEBUG: Systemd command: {systemd_command}")
    
    if dry_run:
        print(f"[DRY RUN] Would execute commands:")
        print(f"  1. Create debug pod on {node_name}")
        print(f"  2. RETIS collection: {systemd_command}")
        print(f"  3. Status check: systemctl status RETIS")
        return True
    
    if not debug_manager:
        raise Exception("debug_manager is required for Kubernetes-native operations")
    
    try:
        # Create debug pod and execute RETIS command
        with debug_manager.debug_pod_context(node_name) as pod_name:
            print(f"Executing RETIS collection command via debug pod...")
            
            # Execute the systemd-run command
            success, stdout, stderr = debug_manager.execute_command(
                node_name, systemd_command, use_chroot=True, timeout=300
            )
            
            if not success:
                print(f"✗ RETIS collection command failed on {node_name}")
                if stderr:
                    print("Error output:")
                    print(stderr)
                return False
            else:
                print(f"✓ RETIS collection command completed successfully on {node_name}")
                if stdout.strip():
                    print("Output:")
                    print(stdout)
            
            # Check the status of the RETIS systemd unit
            print(f"Checking RETIS systemd unit status on {node_name}...")
            status_success, status_stdout, status_stderr = debug_manager.execute_command(
                node_name, "systemctl status RETIS", use_chroot=True, timeout=60
            )
            
            # Parse the status output to determine if the unit actually succeeded
            unit_status = "unknown"
            unit_failed = False
            
            if status_stdout:
                print("Status output:")
                print(status_stdout)
                
                # Look for key indicators in the status output
                status_output = status_stdout.lower()
                if "active: failed" in status_output or "failed" in status_output:
                    unit_failed = True
                    unit_status = "failed"
                elif "active: active" in status_output:
                    unit_status = "running"
                elif "active: inactive" in status_output and "exited" in status_output:
                    # Check if it completed successfully (exit code 0)
                    if "code=exited, status=0" in status_output:
                        unit_status = "completed successfully"
                    else:
                        unit_status = "completed with errors"
                        unit_failed = True
            
            if status_stderr:
                print("Status error output:")
                print(status_stderr)
            
            if unit_failed:
                print(f"✗ RETIS systemd unit failed on {node_name} (status: {unit_status})")
                return False
            elif unit_status == "running":
                print(f"✓ RETIS systemd unit is running on {node_name}")
                return True
            elif unit_status == "completed successfully":
                print(f"✓ RETIS systemd unit completed successfully on {node_name}")
                return True
            else:
                print(f"⚠ RETIS systemd unit status unclear on {node_name} (status: {unit_status})")
                return False
        
    except Exception as e:
        print(f"✗ Error running command on {node_name}: {e}")
        return False

def print_retis_events(file_paths):
    """Print RETIS events from result files using the retis Python library."""
    if not RETIS_ANALYSIS_AVAILABLE:
        print("✗ RETIS Python library is not available. Please install it with: pip install retis")
        return False
    
    if not file_paths:
        print("✗ No files specified for analysis")
        return False
    
    print(f"Reading events from {len(file_paths)} RETIS result file(s)...")
    
    for file_path in file_paths:
        print(f"\n=== Processing file: {file_path} ===")
        
        if not os.path.exists(file_path):
            print(f"⚠ File not found: {file_path}")
            continue
        
        try:
            reader = EventFile(file_path)
            event_count = 0
            
            for event in reader.events():
                print(event)
                event_count += 1
            
            print(f"\n✓ Processed {event_count} events from {file_path}")
            
        except Exception as e:
            print(f"✗ Error reading {file_path}: {e}")
            continue
    
    return True

def main():
    """Main function to get nodes and run RETIS collection."""
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Run RETIS collection on OpenShift worker nodes with filtering options",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # RETIS collection (runs in dry-run mode by default)
  python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker-1"        # exact or substring match
  python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker*"         # wildcard pattern
  python3 arc.py --kubeconfig /path/to/kubeconfig --workload-filter "ovn"
  python3 arc.py --kubeconfig ~/.kube/config --node-filter "compute" --workload-filter "pod.*networking"
  
  # Actually execute RETIS collection (use --start)
  python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker.*" --start
  python3 arc.py --kubeconfig ~/.kube/config --workload-filter "ovn" --start
  
  # Custom RETIS parameters (dry-run by default)
  python3 arc.py --kubeconfig ~/.kube/config --output-file trace.json --filter-packet "tcp port 443"
  python3 arc.py --kubeconfig ~/.kube/config --no-ovs-track --no-stack --filter-packet "udp port 53"
  python3 arc.py --kubeconfig ~/.kube/config --retis-extra-args "--max-events 10000"
  
  # Custom RETIS command (overrides all other RETIS options)
  python3 arc.py --kubeconfig ~/.kube/config --retis-command "collect -o custom.json --max-events 5000" --start
  python3 arc.py --kubeconfig ~/.kube/config --retis-command "profile -o profile.json -t 30" --start
  
  # Infrastructure options
  python3 arc.py --kubeconfig ~/.kube/config --retis-image "custom-registry/retis:latest" --start
  python3 arc.py --kubeconfig ~/.kube/config --retis-tag "v1.6.0" --start
  python3 arc.py --kubeconfig ~/.kube/config --working-directory /tmp --dry-run
  
  # Stop operations (execute normally)
  python3 arc.py --kubeconfig ~/.kube/config --stop --parallel
  python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker" --stop --dry-run  # preview only
  
  # Reset failed operations (execute normally)
  python3 arc.py --kubeconfig ~/.kube/config --reset-failed --parallel
  python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker" --reset-failed --dry-run  # preview only
  
  # Download results (execute normally)
  python3 arc.py --kubeconfig ~/.kube/config --download-results
  python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker-2*" --download-results --dry-run  # preview only
  
  # Print RETIS events (no Kubernetes connection required, downloads results as arc_*_events.json)
  python3 arc.py --analyze                                              # auto-discover arc_*_events.json files
  python3 arc.py --analyze --analysis-files arc_worker-1_events.json arc_worker-2_events.json
  python3 arc.py --analyze --analysis-files retis.data
  
  # Interactive mode
  python3 arc.py  # Will prompt for kubeconfig path
        """
    )
    
    parser.add_argument(
        '--kubeconfig', '-k',
        help='Path to the kubeconfig file (will prompt if not provided)',
        type=str
    )
    parser.add_argument(
        '--node-filter', '-n',
        help='Filter nodes by name. Supports: exact match, substring match, or glob patterns with wildcards (e.g., "worker-1", "worker", "worker*", "*worker*")',
        type=str
    )
    parser.add_argument(
        '--workload-filter', '-w',
        help='Regular expression to filter nodes by workload running on them (e.g., "ovn", "nginx")',
        type=str
    )
    parser.add_argument(
        '--retis-image',
        help='RETIS container image to use (default: image-registry.openshift-image-registry.svc:5000/default/retis)',
        default='image-registry.openshift-image-registry.svc:5000/default/retis',
        type=str
    )
    parser.add_argument(
        '--retis-tag',
        help='RETIS version tag to use (default: v1.5.2)',
        default='v1.5.2',
        type=str
    )
    parser.add_argument(
        '--working-directory',
        help='Working directory for the RETIS collection (default: /var/tmp)',
        default='/var/tmp',
        type=str
    )
    parser.add_argument(
        '--dry-run',
        help='Show what commands would be executed without running them (default for RETIS collection)',
        action='store_true'
    )
    parser.add_argument(
        '--start',
        help='Actually execute RETIS collection (overrides default dry-run behavior for collection)',
        action='store_true'
    )
    parser.add_argument(
        '--parallel',
        help='Run RETIS collection on all nodes in parallel (default: sequential)',
        action='store_true'
    )
    parser.add_argument(
        '--stop',
        help='Stop RETIS collection on filtered nodes',
        action='store_true'
    )
    parser.add_argument(
        '--reset-failed',
        help='Reset failed RETIS systemd units on filtered nodes',
        action='store_true'
    )
    parser.add_argument(
        '--download-results',
        help='Download all events.json files from filtered nodes to local machine',
        action='store_true'
    )
    # RETIS collection configuration parameters
    parser.add_argument(
        '--output-file', '-o',
        help='Output file name for RETIS collection (default: events.json)',
        default='events.json',
        type=str
    )
    parser.add_argument(
        '--allow-system-changes',
        help='Allow RETIS to make system changes (default: enabled)',
        action='store_true',
        default=True
    )
    parser.add_argument(
        '--no-allow-system-changes',
        help='Disable system changes for RETIS collection',
        action='store_true'
    )
    parser.add_argument(
        '--ovs-track',
        help='Enable OVS tracking (default: enabled)',
        action='store_true',
        default=True
    )
    parser.add_argument(
        '--no-ovs-track',
        help='Disable OVS tracking',
        action='store_true'
    )
    parser.add_argument(
        '--stack',
        help='Enable stack trace collection (default: enabled)',
        action='store_true',
        default=True
    )
    parser.add_argument(
        '--no-stack',
        help='Disable stack trace collection',
        action='store_true'
    )
    parser.add_argument(
        '--probe-stack',
        help='Enable probe stack collection (default: enabled)',
        action='store_true',
        default=True
    )
    parser.add_argument(
        '--no-probe-stack',
        help='Disable probe stack collection',
        action='store_true'
    )
    parser.add_argument(
        '--filter-packet',
        help='Packet filter expression (default: "tcp port 8080 or tcp port 8081")',
        default='tcp port 8080 or tcp port 8081',
        type=str
    )
    parser.add_argument(
        '--retis-extra-args',
        help='Additional arguments to pass to retis collect command',
        default='',
        type=str
    )
    parser.add_argument(
        '--retis-command',
        help='Complete RETIS command string (overrides all other RETIS options)',
        type=str
    )
    parser.add_argument(
        '--skip-tls-verification',
        help='Skip TLS certificate verification when connecting to Kubernetes API',
        action='store_true'
    )
    parser.add_argument(
        '--analyze',
        help='Print events from downloaded RETIS result files',
        action='store_true'
    )
    parser.add_argument(
        '--analysis-files',
        help='Specific RETIS result files to read (space-separated). If not provided, will read all arc_*_events.json files in current directory',
        nargs='*',
        type=str
    )
    
    args = parser.parse_args()
    
    # Set dry-run behavior based on operation type
    # Main RETIS collection defaults to dry-run, utility operations execute normally
    is_utility_operation = (getattr(args, 'stop', False) or 
                           getattr(args, 'reset_failed', False) or 
                           getattr(args, 'download_results', False))
    
    if args.start:
        # --start always overrides --dry-run when both are present
        args.dry_run = False
    elif not args.dry_run and not is_utility_operation:
        # Default to dry-run only for main RETIS collection operation
        args.dry_run = True
    # For utility operations, keep the original --dry-run value (False by default)
    
    # Process boolean flag conflicts
    if args.no_allow_system_changes:
        args.allow_system_changes = False
    if args.no_ovs_track:
        args.ovs_track = False
    if args.no_stack:
        args.stack = False
    if args.no_probe_stack:
        args.probe_stack = False
    
    # Validate arguments
    if args.stop and getattr(args, 'reset_failed', False):
        print("Error: --stop and --reset-failed cannot be used together")
        return
    
    if args.stop and getattr(args, 'download_results', False):
        print("Error: --stop and --download-results cannot be used together")
        return
    
    if getattr(args, 'reset_failed', False) and getattr(args, 'download_results', False):
        print("Error: --reset-failed and --download-results cannot be used together")
        return
    
    if getattr(args, 'analyze', False) and (args.stop or getattr(args, 'reset_failed', False) or getattr(args, 'download_results', False)):
        print("Error: --analyze cannot be used with --stop, --reset-failed, or --download-results")
        return
    
    if args.stop:
        if args.retis_image != 'image-registry.openshift-image-registry.svc:5000/default/retis':
            print("Warning: --retis-image is ignored when using --stop")
        if args.retis_tag != 'v1.5.2':
            print("Warning: --retis-tag is ignored when using --stop")
        if args.working_directory != '/var/tmp':
            print("Warning: --working-directory is ignored when using --stop")
        # These retis-specific arguments are ignored during stop
        ignored_args = []
        if args.output_file != 'events.json':
            ignored_args.append('--output-file')
        if not args.allow_system_changes:
            ignored_args.append('--no-allow-system-changes')
        if not args.ovs_track:
            ignored_args.append('--no-ovs-track')
        if not args.stack:
            ignored_args.append('--no-stack')
        if not args.probe_stack:
            ignored_args.append('--no-probe-stack')
        if args.filter_packet != 'tcp port 8080 or tcp port 8081':
            ignored_args.append('--filter-packet')
        if args.retis_extra_args:
            ignored_args.append('--retis-extra-args')
        
        if ignored_args:
            print(f"Warning: RETIS collection arguments are ignored when using --stop: {', '.join(ignored_args)}")
    
    if getattr(args, 'reset_failed', False):
        if args.retis_image != 'image-registry.openshift-image-registry.svc:5000/default/retis':
            print("Warning: --retis-image is ignored when using --reset-failed")
        if args.retis_tag != 'v1.5.2':
            print("Warning: --retis-tag is ignored when using --reset-failed")
        if args.working_directory != '/var/tmp':
            print("Warning: --working-directory is ignored when using --reset-failed")
        # These retis-specific arguments are ignored during reset-failed
        ignored_args = []
        if args.output_file != 'events.json':
            ignored_args.append('--output-file')
        if not args.allow_system_changes:
            ignored_args.append('--no-allow-system-changes')
        if not args.ovs_track:
            ignored_args.append('--no-ovs-track')
        if not args.stack:
            ignored_args.append('--no-stack')
        if not args.probe_stack:
            ignored_args.append('--no-probe-stack')
        if args.filter_packet != 'tcp port 8080 or tcp port 8081':
            ignored_args.append('--filter-packet')
        if args.retis_extra_args:
            ignored_args.append('--retis-extra-args')
        
        if ignored_args:
            print(f"Warning: RETIS collection arguments are ignored when using --reset-failed: {', '.join(ignored_args)}")
    
    if getattr(args, 'download_results', False):
        if args.retis_image != 'image-registry.openshift-image-registry.svc:5000/default/retis':
            print("Warning: --retis-image is ignored when using --download-results")
        if args.retis_tag != 'v1.5.2':
            print("Warning: --retis-tag is ignored when using --download-results")
        # Note: --working-directory and --output-file are actually used by download-results
        # These retis-specific arguments are ignored during download-results
        ignored_args = []
        if not args.allow_system_changes:
            ignored_args.append('--no-allow-system-changes')
        if not args.ovs_track:
            ignored_args.append('--no-ovs-track')
        if not args.stack:
            ignored_args.append('--no-stack')
        if not args.probe_stack:
            ignored_args.append('--no-probe-stack')
        if args.filter_packet != 'tcp port 8080 or tcp port 8081':
            ignored_args.append('--filter-packet')
        if args.retis_extra_args:
            ignored_args.append('--retis-extra-args')
        
        if ignored_args:
            print(f"Warning: RETIS collection arguments are ignored when using --download-results: {', '.join(ignored_args)}")
    
    # Validate that at least one filter is provided if the user wants to be specific
    if not args.node_filter and not args.workload_filter:
        print("Warning: No filters specified. This will run on ALL worker nodes in the cluster.")
        print("Use --node-filter and/or --workload-filter to limit the nodes.")
        
        if not args.dry_run:
            confirmation = input("Continue with all worker nodes? (y/N): ").strip().lower()
            if confirmation not in ['y', 'yes']:
                print("Operation cancelled.")
                return

    # --- Handle analysis operation (doesn't require Kubernetes connection) ---
    if getattr(args, 'analyze', False):
        print("Starting RETIS events printing...")
        
        # Determine which files to read
        analysis_files = []
        if getattr(args, 'analysis_files', None):
            # Use specified files
            analysis_files = args.analysis_files
            print(f"Reading specified files: {analysis_files}")
        else:
            # Auto-discover files matching pattern arc_*_events.json
            import glob
            pattern = "arc_*_events.json"
            analysis_files = glob.glob(pattern)
            if analysis_files:
                print(f"Auto-discovered {len(analysis_files)} result files: {analysis_files}")
            else:
                print(f"No files found matching pattern '{pattern}' in current directory")
                print("Use --analysis-files to specify files explicitly, or use --download-results first to download files")
                return
        
        # Print events
        success = print_retis_events(file_paths=analysis_files)
        
        if success:
            print("\nEvent printing completed successfully!")
        else:
            print("\nEvent printing failed!")
            
        return

    # Get kubeconfig path
    kubeconfig_path = get_kubeconfig_path(args)

    # --- Load Kubernetes Configuration ---
    try:
        # Try to load in-cluster config first
        config.load_incluster_config()
        print("Loaded in-cluster Kubernetes configuration.")
    except config.ConfigException:
        try:
            # Use the specified kubeconfig file path
            config.load_kube_config(config_file=kubeconfig_path)
            print(f"Loaded kubeconfig from: {kubeconfig_path}")
        except config.ConfigException as e:
            print(f"Could not load kubeconfig from {kubeconfig_path}")
            print(f"Error: {e}")
            try:
                # Fallback to default kube-config file location
                config.load_kube_config()
                print("Loaded default kube-config.")
            except config.ConfigException:
                print("Could not locate a valid kubeconfig file or in-cluster config.")
                print("Please ensure your kubeconfig file exists and is properly configured.")
                return
        except FileNotFoundError:
            print(f"Kubeconfig file not found at: {kubeconfig_path}")
            print("Please verify the file path exists.")
            return

    # --- Handle TLS verification settings ---
    if args.skip_tls_verification:
        print("Warning: Skipping TLS certificate verification. This is insecure and should only be used for testing.")
        
        # Get the current configuration
        configuration = client.Configuration.get_default_copy()
        
        # Disable SSL verification
        configuration.verify_ssl = False
        
        # Disable urllib3 SSL warnings when verification is disabled
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        # Set the configuration as default
        client.Configuration.set_default(configuration)

    # --- Create Kubernetes API client ---
    core_v1 = client.CoreV1Api()
    
    # --- Create Debug Pod Manager ---
    debug_manager = KubernetesDebugPodManager(core_v1, namespace="default")

    # Test the connection
    try:
        print("Testing connection to Kubernetes cluster...")
        version = core_v1.get_api_resources()
        print("✓ Successfully connected to Kubernetes cluster.")
    except Exception as e:
        print(f"✗ Failed to connect to Kubernetes cluster: {e}")
        print("Please verify your kubeconfig is valid and the cluster is accessible.")
        return

    # --- Get nodes using Kubernetes API ---
    nodes = get_nodes_from_kubernetes(core_v1, name_filter=args.node_filter, workload_filter=args.workload_filter)
    
    if not nodes:
        print("No nodes found matching the specified filters. Exiting.")
        return
    
    # --- Handle stop operation ---
    if args.stop:
        print(f"\nPreparing to stop RETIS collection on {len(nodes)} nodes...")
        
        if args.dry_run:
            print("\n[DRY RUN] The following stop commands would be executed:")
        
        # Stop RETIS on each node
        if args.parallel:
            print("\nStopping RETIS collection in parallel mode...")
            import concurrent.futures
            
            def stop_with_progress(node):
                return stop_retis_on_node(node, debug_manager, args.dry_run)
            
            success_count = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(nodes), 5)) as executor:
                future_to_node = {executor.submit(stop_with_progress, node): node for node in nodes}
                
                for future in concurrent.futures.as_completed(future_to_node):
                    node = future_to_node[future]
                    try:
                        success = future.result()
                        if success:
                            success_count += 1
                    except Exception as e:
                        print(f"✗ Exception occurred stopping RETIS on node {node}: {e}")
        else:
            print("\nStopping RETIS collection sequentially...")
            success_count = 0
            for i, node in enumerate(nodes, 1):
                print(f"\n--- Stopping RETIS on node {i}/{len(nodes)}: {node} ---")
                success = stop_retis_on_node(node, debug_manager, args.dry_run)
                if success:
                    success_count += 1
        
        # Summary for stop operation
        print(f"\n{'=' * 50}")
        print("RETIS Stop Summary")
        print(f"{'=' * 50}")
        print(f"Total nodes: {len(nodes)}")
        print(f"Successfully stopped: {success_count}")
        print(f"Failed to stop: {len(nodes) - success_count}")
        
        if args.dry_run:
            print("\n[DRY RUN] No actual commands were executed.")
        else:
            if success_count == len(nodes):
                print("\n✓ RETIS collection stopped on all nodes!")
            elif success_count > 0:
                print(f"\n⚠ RETIS collection stopped on {success_count}/{len(nodes)} nodes.")
            else:
                print("\n✗ Failed to stop RETIS collection on all nodes.")
        
        print("Script finished.")
        return
    
    # --- Handle reset-failed operation ---
    if getattr(args, 'reset_failed', False):
        print(f"\nPreparing to reset failed RETIS units on {len(nodes)} nodes...")
        
        if args.dry_run:
            print("\n[DRY RUN] The following reset-failed commands would be executed:")
        
        # Reset failed RETIS on each node
        if args.parallel:
            print("\nResetting failed RETIS units in parallel mode...")
            import concurrent.futures
            
            def reset_with_progress(node):
                return reset_failed_retis_on_node(node, debug_manager, args.dry_run)
            
            success_count = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(nodes), 5)) as executor:
                future_to_node = {executor.submit(reset_with_progress, node): node for node in nodes}
                
                for future in concurrent.futures.as_completed(future_to_node):
                    node = future_to_node[future]
                    try:
                        success = future.result()
                        if success:
                            success_count += 1
                    except Exception as e:
                        print(f"✗ Exception occurred resetting failed RETIS on node {node}: {e}")
        else:
            print("\nResetting failed RETIS units sequentially...")
            success_count = 0
            for i, node in enumerate(nodes, 1):
                print(f"\n--- Resetting failed RETIS on node {i}/{len(nodes)}: {node} ---")
                success = reset_failed_retis_on_node(node, debug_manager, args.dry_run)
                if success:
                    success_count += 1
        
        # Summary for reset-failed operation
        print(f"\n{'=' * 50}")
        print("RETIS Reset-Failed Summary")
        print(f"{'=' * 50}")
        print(f"Total nodes: {len(nodes)}")
        print(f"Successfully reset: {success_count}")
        print(f"Failed to reset: {len(nodes) - success_count}")
        
        if args.dry_run:
            print("\n[DRY RUN] No actual commands were executed.")
        else:
            if success_count == len(nodes):
                print("\n✓ RETIS failed units reset on all nodes!")
            elif success_count > 0:
                print(f"\n⚠ RETIS failed units reset on {success_count}/{len(nodes)} nodes.")
            else:
                print("\n✗ Failed to reset RETIS failed units on all nodes.")
        
        print("Script finished.")
        return
    
    # --- Handle download-results operation ---
    if getattr(args, 'download_results', False):
        print(f"\nPreparing to download RETIS results from {len(nodes)} nodes...")
        print(f"Working Directory: {args.working_directory}")
        print(f"Output File: {args.output_file}")
        
        if args.dry_run:
            print("\n[DRY RUN] The following download commands would be executed:")
        
        # Download results from each node
        if args.parallel:
            print("\nDownloading RETIS results in parallel mode...")
            import concurrent.futures
            
            def download_with_progress(node):
                return download_results_from_node(node, args.working_directory, args.output_file, "./", debug_manager, args.dry_run)
            
            success_count = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(nodes), 5)) as executor:
                future_to_node = {executor.submit(download_with_progress, node): node for node in nodes}
                
                for future in concurrent.futures.as_completed(future_to_node):
                    node = future_to_node[future]
                    try:
                        success = future.result()
                        if success:
                            success_count += 1
                    except Exception as e:
                        print(f"✗ Exception occurred downloading results from node {node}: {e}")
        else:
            print("\nDownloading RETIS results sequentially...")
            success_count = 0
            for i, node in enumerate(nodes, 1):
                print(f"\n--- Downloading results from node {i}/{len(nodes)}: {node} ---")
                success = download_results_from_node(node, args.working_directory, args.output_file, "./", debug_manager, args.dry_run)
                if success:
                    success_count += 1
        
        # Summary for download operation
        print(f"\n{'=' * 50}")
        print("RETIS Download Summary")
        print(f"{'=' * 50}")
        print(f"Total nodes: {len(nodes)}")
        print(f"Successfully downloaded: {success_count}")
        print(f"Failed to download: {len(nodes) - success_count}")
        
        if args.dry_run:
            print("\n[DRY RUN] No actual commands were executed.")
        else:
            if success_count == len(nodes):
                print("\n✓ All RETIS results downloaded successfully!")
            elif success_count > 0:
                print(f"\n⚠ RETIS results downloaded from {success_count}/{len(nodes)} nodes.")
                print("Files downloaded to current directory with node name prefix.")
            else:
                print("\n✗ Failed to download RETIS results from all nodes.")
        
        print("Script finished.")
        return
    
    print(f"\nPreparing to run RETIS collection on {len(nodes)} nodes...")
    print(f"RETIS Image: {args.retis_image}")
    print(f"RETIS Tag: {args.retis_tag}")
    print(f"Working Directory: {args.working_directory}")
    
    if args.dry_run:
        print("\n[DRY RUN] The following commands would be executed:")
    
    # --- Download retis_in_container.sh script locally ---
    print(f"\n--- Downloading retis_in_container.sh script locally ---")
    local_script_path = None
    
    if not args.dry_run:
        local_script_path = download_retis_script_locally()
        if not local_script_path:
            print("✗ Failed to download script locally. Cannot proceed.")
            return
    else:
        print("[DRY RUN] Would download retis_in_container.sh locally")
        local_script_path = "/tmp/dummy_script_path"  # placeholder for dry run
    
    try:
        # --- Setup retis_in_container.sh script on each node ---
        print(f"\n--- Setting up retis_in_container.sh script on {len(nodes)} nodes ---")
        setup_success_count = 0
        setup_failed_nodes = []
        
        for i, node in enumerate(nodes, 1):
            print(f"\n--- Setting up script on node {i}/{len(nodes)}: {node} ---")
            setup_success = setup_script_on_node(node, args.working_directory, local_script_path, debug_manager, args.dry_run)
            if setup_success:
                setup_success_count += 1
            else:
                setup_failed_nodes.append(node)
        
        if setup_failed_nodes and not args.dry_run:
            print(f"\n⚠ Script setup failed on {len(setup_failed_nodes)} nodes:")
            for node in setup_failed_nodes:
                print(f"  - {node}")
            print("RETIS collection will only run on nodes where script setup succeeded.")
            # Remove failed nodes from the list
            nodes = [node for node in nodes if node not in setup_failed_nodes]
            if not nodes:
                print("No nodes available for RETIS collection. Exiting.")
                return
        
        print(f"\n--- Script setup complete: {setup_success_count}/{len(nodes) + len(setup_failed_nodes)} nodes successful ---")
        
        # --- Prepare RETIS arguments ---
        retis_args = {
            'output_file': args.output_file,
            'allow_system_changes': args.allow_system_changes,
            'ovs_track': args.ovs_track,
            'stack': args.stack,
            'probe_stack': args.probe_stack,
            'filter_packet': args.filter_packet,
            'retis_extra_args': args.retis_extra_args,
            'retis_tag': args.retis_tag
        }
        
        # Use custom RETIS command if provided
        custom_retis_cmd = getattr(args, 'retis_command', None)
        
        # Warn if custom command is used with individual RETIS parameters
        if custom_retis_cmd:
            print(f"Using custom RETIS command: {custom_retis_cmd}")
            ignored_args = []
            if args.output_file != 'events.json':
                ignored_args.append('--output-file')
            if not args.allow_system_changes:
                ignored_args.append('--no-allow-system-changes')
            if not args.ovs_track:
                ignored_args.append('--no-ovs-track')
            if not args.stack:
                ignored_args.append('--no-stack')
            if not args.probe_stack:
                ignored_args.append('--no-probe-stack')
            if args.filter_packet != 'tcp port 8080 or tcp port 8081':
                ignored_args.append('--filter-packet')
            if args.retis_extra_args:
                ignored_args.append('--retis-extra-args')
            
            if ignored_args:
                print(f"Warning: Individual RETIS parameters are ignored when using --retis-command: {', '.join(ignored_args)}")
                print("The custom command will be used as-is.")
        
        # --- Run RETIS collection on each node ---
        if args.parallel:
            print("\nRunning RETIS collection in parallel mode...")
            import concurrent.futures
            import threading
            
            def run_with_progress(node):
                return run_retis_on_node(node, args.retis_image, args.working_directory, retis_args, custom_retis_cmd, debug_manager, args.dry_run)
            
            success_count = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(nodes), 5)) as executor:
                future_to_node = {executor.submit(run_with_progress, node): node for node in nodes}
                
                for future in concurrent.futures.as_completed(future_to_node):
                    node = future_to_node[future]
                    try:
                        success = future.result()
                        if success:
                            success_count += 1
                    except Exception as e:
                        print(f"✗ Exception occurred for node {node}: {e}")
        else:
            print("\nRunning RETIS collection sequentially...")
            success_count = 0
            for i, node in enumerate(nodes, 1):
                print(f"\n--- Processing node {i}/{len(nodes)} ---")
                success = run_retis_on_node(node, args.retis_image, args.working_directory, retis_args, custom_retis_cmd, debug_manager, args.dry_run)
                if success:
                    success_count += 1
        
        # --- Summary ---
        print(f"\n{'=' * 50}")
        print("RETIS Collection Summary")
        print(f"{'=' * 50}")
        print(f"Total nodes: {len(nodes)}")
        print(f"Successful: {success_count}")
        print(f"Failed: {len(nodes) - success_count}")
        
        if args.dry_run:
            print("\n[DRY RUN] No actual commands were executed.")
        else:
            if success_count == len(nodes):
                print("\n✓ All RETIS collections completed successfully!")
            elif success_count > 0:
                print(f"\n⚠ {success_count}/{len(nodes)} RETIS collections completed successfully.")
            else:
                print("\n✗ All RETIS collections failed.")
        
        print("Script finished.")
        
    finally:
        # Clean up debug pods
        try:
            debug_manager.cleanup_all_pods()
        except Exception as e:
            print(f"Warning: Failed to clean up debug pods: {e}")
        
        # Clean up the temporary script file
        if local_script_path and not args.dry_run and local_script_path != "/tmp/dummy_script_path":
            try:
                os.unlink(local_script_path)
                print(f"Cleaned up temporary file: {local_script_path}")
            except Exception as e:
                print(f"Warning: Failed to clean up temporary file {local_script_path}: {e}")

if __name__ == "__main__":
    main() 