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
from kubernetes import client, config

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
        
        # Apply name filtering if specified
        filtered_nodes = all_nodes
        if name_filter:
            filtered_nodes = [node for node in filtered_nodes if re.search(name_filter, node, re.IGNORECASE)]
            print(f"✓ After name filter '{name_filter}': {len(filtered_nodes)} nodes")
        
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

def setup_script_on_node(node_name, working_directory, local_script_path, dry_run=False):
    """Copy the retis_in_container.sh script to a specific node and set permissions if needed."""
    print(f"Checking retis_in_container.sh on node {node_name}...")
    
    if dry_run:
        print(f"[DRY RUN] Would check if {working_directory}/retis_in_container.sh exists with correct permissions")
        print(f"[DRY RUN] Would create working directory {working_directory} on {node_name} if needed")
        print(f"[DRY RUN] Would copy {local_script_path} to {node_name}:{working_directory}/retis_in_container.sh if needed")
        print(f"[DRY RUN] Would set executable permissions on the script if needed")
        return True
    
    try:
        # First, check if the script already exists with correct permissions
        check_cmd = f'oc debug node/{node_name} -- chroot /host ls -la {working_directory}/retis_in_container.sh'
        print(f"Checking existing script on {node_name}...")
        
        check_result = subprocess.run(check_cmd, shell=True, capture_output=True, text=True, timeout=30)
        
        script_exists = False
        script_executable = False
        
        if check_result.returncode == 0 and check_result.stdout:
            script_exists = True
            # Check if the script is executable (look for 'x' in permissions)
            permissions = check_result.stdout.strip()
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
        mkdir_cmd = f'oc debug node/{node_name} -- chroot /host mkdir -p {working_directory}'
        print(f"Ensuring directory exists on {node_name}...")
        
        mkdir_result = subprocess.run(mkdir_cmd, shell=True, capture_output=True, text=True, timeout=30)
        
        if mkdir_result.returncode != 0:
            print(f"⚠ Warning: Failed to create directory on {node_name} (might already exist)")
        
        # Only copy script if it doesn't exist
        if not script_exists:
            print(f"Copying script to {node_name}...")
            
            # Start a debug pod and get its name for copying
            debug_cmd = f'oc debug node/{node_name} --to-namespace=default -- sleep 300'
            print(f"Starting debug pod on {node_name}...")
            
            # Run the debug command in background and capture the pod name
            debug_process = subprocess.Popen(debug_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            
            # Wait a moment for the pod to start
            time.sleep(10)
            
            # Get the debug pod name
            get_pod_cmd = f'oc get pods -n default --no-headers | grep {node_name.split(".")[0]} | grep debug | head -1 | awk \'{{print $1}}\''
            pod_result = subprocess.run(get_pod_cmd, shell=True, capture_output=True, text=True, timeout=30)
            
            if pod_result.returncode != 0 or not pod_result.stdout.strip():
                print(f"✗ Failed to find debug pod for {node_name}")
                debug_process.terminate()
                return False
            
            debug_pod_name = pod_result.stdout.strip()
            print(f"Using debug pod: {debug_pod_name}")
            
            # Copy the script to the node
            copy_cmd = f'oc cp {local_script_path} default/{debug_pod_name}:/host{working_directory}/retis_in_container.sh'
            print(f"Copying script: {copy_cmd}")
            
            copy_result = subprocess.run(copy_cmd, shell=True, capture_output=True, text=True, timeout=60)
            
            # Terminate the debug pod
            debug_process.terminate()
            
            if copy_result.returncode != 0:
                print(f"✗ Failed to copy script to {node_name}")
                if copy_result.stderr:
                    print(f"Copy error: {copy_result.stderr}")
                return False
            
            print(f"✓ Script copied to {node_name}")
        
        # Set executable permissions if script is not executable
        if not script_executable:
            print(f"Setting executable permissions on {node_name}...")
            chmod_cmd = f'oc debug node/{node_name} -- chroot /host chmod a+x {working_directory}/retis_in_container.sh'
            
            chmod_result = subprocess.run(chmod_cmd, shell=True, capture_output=True, text=True, timeout=30)
            
            if chmod_result.returncode != 0:
                print(f"✗ Failed to set permissions on {node_name}")
                if chmod_result.stderr:
                    print(f"Chmod error: {chmod_result.stderr}")
                return False
            
            print(f"✓ Executable permissions set on {node_name}")
        
        # Final verification
        verify_cmd = f'oc debug node/{node_name} -- chroot /host ls -la {working_directory}/retis_in_container.sh'
        verify_result = subprocess.run(verify_cmd, shell=True, capture_output=True, text=True, timeout=30)
        
        if verify_result.returncode == 0:
            print(f"✓ Script setup complete on {node_name}")
            if verify_result.stdout:
                print(f"Final file info: {verify_result.stdout.strip()}")
            return True
        else:
            print(f"✗ Script verification failed on {node_name}")
            return False
        
    except subprocess.TimeoutExpired:
        print(f"✗ Timeout setting up script on {node_name}")
        return False
    except Exception as e:
        print(f"✗ Error setting up script on {node_name}: {e}")
        return False

def stop_retis_on_node(node_name, dry_run=False):
    """Stop the RETIS systemd unit on a specific node."""
    print(f"Stopping RETIS collection on node: {node_name}")
    
    if dry_run:
        print(f"[DRY RUN] Would execute command:")
        print(f"  Stop RETIS: oc debug node/{node_name} -- chroot /host systemctl stop RETIS")
        return True
    
    try:
        # Stop the RETIS systemd unit
        stop_command_str = f'oc debug node/{node_name} -- chroot /host systemctl stop RETIS'
        print(f"Executing stop command...")
        print(f"DEBUG: Stop command: {stop_command_str}")
        
        stop_result = subprocess.run(stop_command_str, shell=True, capture_output=True, text=True, timeout=60)
        
        if stop_result.returncode != 0:
            print(f"✗ RETIS stop command failed on {node_name} (exit code: {stop_result.returncode})")
            if stop_result.stderr:
                print("Stop error output:")
                print(stop_result.stderr)
            if stop_result.stdout:
                print("Stop output:")
                print(stop_result.stdout)
            return False
        else:
            print(f"✓ RETIS systemd unit successfully stopped on {node_name}")
            if stop_result.stdout:
                print("Stop output:")
                print(stop_result.stdout)
            return True
        
    except subprocess.TimeoutExpired:
        print(f"✗ RETIS stop command timed out on {node_name}")
        return False
    except FileNotFoundError:
        print("✗ 'oc' command not found. Please ensure OpenShift CLI is installed and in PATH.")
        return False
    except Exception as e:
        print(f"✗ Error stopping RETIS on {node_name}: {e}")
        return False

def run_retis_on_node(node_name, retis_image, working_directory, retis_args=None, dry_run=False):
    """Run the oc debug command with RETIS collection on a specific node."""
    
    # Set default retis_args if not provided (for backwards compatibility)
    if retis_args is None:
        retis_args = {
            'output_file': 'events.json',
            'allow_system_changes': True,
            'ovs_track': True,
            'stack': True,
            'probe_stack': True,
            'filter_packet': 'tcp port 8080 or tcp port 8081',
            'retis_extra_args': ''
        }
    
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
    
    # Construct the shell command that will be executed after 'sh -c'
    # Use full path to the script since we downloaded it to the working directory
    shell_command = f"export RETIS_IMAGE='{retis_image}'; {working_directory}/retis_in_container.sh {retis_cmd_str}"
    
    # Construct the command as a string for shell=True execution (like manual command)
    command_str = f'oc debug node/{node_name} -- chroot /host systemd-run --unit="RETIS" --working-directory={working_directory} sh -c "{shell_command}"'
    
    print(f"\nRunning RETIS collection on node: {node_name}")
    print(f"Working directory: {working_directory}")
    print(f"RETIS Image: {retis_image}")
    print(f"RETIS Arguments: {retis_cmd_str}")
    print(f"DEBUG: Shell command: {shell_command}")
    print(f"DEBUG: Full command: {command_str}")
    
    if dry_run:
        print(f"[DRY RUN] Would execute commands:")
        print(f"  1. RETIS collection: {command_str}")
        
        # Display the status check command
        status_command_str = f'oc debug node/{node_name} -- chroot /host systemctl status RETIS'
        print(f"  2. Status check: {status_command_str}")
        return True
    
    try:
        print(f"Executing RETIS collection command...")
        print(f"DEBUG: Command string: {command_str}")
        result = subprocess.run(command_str, shell=True, capture_output=True, text=True, timeout=300)
        
        if result.returncode == 0:
            print(f"✓ RETIS collection command completed successfully on {node_name}")
            if result.stdout:
                print("Output:")
                print(result.stdout)
        else:
            print(f"✗ RETIS collection command failed on {node_name} (exit code: {result.returncode})")
            if result.stderr:
                print("Error output:")
                print(result.stderr)
            return False
        
        # Check the status of the RETIS systemd unit
        print(f"Checking RETIS systemd unit status on {node_name}...")
        status_command_str = f'oc debug node/{node_name} -- chroot /host systemctl status RETIS'
        
        status_result = subprocess.run(status_command_str, shell=True, capture_output=True, text=True, timeout=60)
        
        # Parse the status output to determine if the unit actually succeeded
        unit_status = "unknown"
        unit_failed = False
        
        if status_result.stdout:
            print("Status output:")
            print(status_result.stdout)
            
            # Look for key indicators in the status output
            status_output = status_result.stdout.lower()
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
        
        if status_result.stderr:
            print("Status error output:")
            print(status_result.stderr)
        
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
        
    except subprocess.TimeoutExpired:
        print(f"✗ RETIS collection or status check timed out on {node_name}")
        return False
    except FileNotFoundError:
        print("✗ 'oc' command not found. Please ensure OpenShift CLI is installed and in PATH.")
        return False
    except Exception as e:
        print(f"✗ Error running command on {node_name}: {e}")
        return False

def main():
    """Main function to get nodes and run RETIS collection."""
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Run RETIS collection on OpenShift worker nodes with filtering options",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage
  python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker.*"
  python3 arc.py --kubeconfig /path/to/kubeconfig --workload-filter "ovn"
  python3 arc.py --kubeconfig ~/.kube/config --node-filter "compute" --workload-filter "pod.*networking"
  
  # Custom RETIS parameters
  python3 arc.py --kubeconfig ~/.kube/config --output-file trace.json --filter-packet "tcp port 443"
  python3 arc.py --kubeconfig ~/.kube/config --no-ovs-track --no-stack --filter-packet "udp port 53"
  python3 arc.py --kubeconfig ~/.kube/config --retis-extra-args "--max-events 10000"
  
  # Infrastructure options
  python3 arc.py --kubeconfig ~/.kube/config --retis-image "custom-registry/retis:latest"
  python3 arc.py --kubeconfig ~/.kube/config --working-directory /tmp --dry-run
  
  # Stop operations
  python3 arc.py --kubeconfig ~/.kube/config --stop --parallel
  python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker" --stop --dry-run
  
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
        help='Regular expression to filter nodes by name (e.g., "worker.*", "compute")',
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
        '--working-directory',
        help='Working directory for the RETIS collection (default: /var/tmp)',
        default='/var/tmp',
        type=str
    )
    parser.add_argument(
        '--dry-run',
        help='Show what commands would be executed without running them',
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
    
    args = parser.parse_args()
    
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
    if args.stop:
        if args.retis_image != 'image-registry.openshift-image-registry.svc:5000/default/retis':
            print("Warning: --retis-image is ignored when using --stop")
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
    
    # Validate that at least one filter is provided if the user wants to be specific
    if not args.node_filter and not args.workload_filter:
        print("Warning: No filters specified. This will run on ALL worker nodes in the cluster.")
        print("Use --node-filter and/or --workload-filter to limit the nodes.")
        
        if not args.dry_run:
            confirmation = input("Continue with all worker nodes? (y/N): ").strip().lower()
            if confirmation not in ['y', 'yes']:
                print("Operation cancelled.")
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

    # --- Create Kubernetes API client ---
    core_v1 = client.CoreV1Api()

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
                return stop_retis_on_node(node, args.dry_run)
            
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
                success = stop_retis_on_node(node, args.dry_run)
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
    
    print(f"\nPreparing to run RETIS collection on {len(nodes)} nodes...")
    print(f"RETIS Image: {args.retis_image}")
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
            setup_success = setup_script_on_node(node, args.working_directory, local_script_path, dry_run=args.dry_run)
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
            'retis_extra_args': args.retis_extra_args
        }
        
        # --- Run RETIS collection on each node ---
        if args.parallel:
            print("\nRunning RETIS collection in parallel mode...")
            import concurrent.futures
            import threading
            
            def run_with_progress(node):
                return run_retis_on_node(node, args.retis_image, args.working_directory, retis_args, args.dry_run)
            
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
                success = run_retis_on_node(node, args.retis_image, args.working_directory, retis_args, args.dry_run)
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
        # Clean up the temporary script file
        if local_script_path and not args.dry_run and local_script_path != "/tmp/dummy_script_path":
            try:
                os.unlink(local_script_path)
                print(f"Cleaned up temporary file: {local_script_path}")
            except Exception as e:
                print(f"Warning: Failed to clean up temporary file {local_script_path}: {e}")

if __name__ == "__main__":
    main() 