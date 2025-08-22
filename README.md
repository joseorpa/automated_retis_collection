# Automated RETIS Collection (ARC)

A Python script for running RETIS network packet collection on OpenShift/Kubernetes worker nodes with advanced filtering capabilities.

## 🎯 Overview

This tool automates the deployment and execution of RETIS (Real-time Traffic Inspection System) on Kubernetes worker nodes. It provides flexible node selection through name patterns and workload filtering, making it easy to target specific nodes for network analysis.

## ✨ Features

- **🔍 Smart Node Filtering**: Filter nodes by glob patterns (`worker-2*`) or running workloads
- **🚀 Parallel Execution**: Run RETIS collection on multiple nodes simultaneously
- **⚙️ Flexible Configuration**: Support for custom RETIS images, tags, and working directories
- **🛡️ Enhanced Safety**: Default dry-run mode for collection, explicit start required
- **📋 Comprehensive Management**: Start, stop, reset-failed, and download operations
- **📥 Results Download**: Automatically download events.json files from all nodes
- **🔧 Custom Commands**: Full control over RETIS commands and arguments
- **🏷️ Version Control**: Configurable RETIS version tags (defaults to stable v1.5.2)
- **🔌 Native Kubernetes Integration**: Uses Kubernetes Python client API for reliable cluster interaction

## 📋 Requirements

- Python 3.6+
- Access to a Kubernetes/OpenShift cluster
- Valid kubeconfig file
- Appropriate RBAC permissions to list nodes and pods

## 🚀 Installation

1. **Clone or download the script:**
   ```bash
   git clone <repository-url>
   cd automated_retis_collection
   ```

2. **Create and activate a virtual environment (recommended):**
   ```bash
   # Create virtual environment
   python3 -m venv venv
   
   # Activate virtual environment
   # On Linux/macOS:
   source venv/bin/activate
   
   # On Windows:
   # venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
   or
   ```bash
   pip install "kubernetes>=18.20.0"
   ```

## 🎮 Usage

### Basic Usage

```bash
# Preview RETIS collection (default dry-run mode)
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker-2*"

# Actually execute RETIS collection (use --start)
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker-2*" --start

# Filter nodes running specific workloads
python3 arc.py --kubeconfig ~/.kube/config --workload-filter "ovn" --start

# Combine both filters
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker*" --workload-filter "networking" --start

# Use without kubeconfig argument (will prompt)
python3 arc.py --node-filter "worker-2*" --start
```

### Advanced Usage

```bash
# Custom RETIS image and version
python3 arc.py --kubeconfig ~/.kube/config --retis-image "registry.example.com/retis:custom" --retis-tag "v1.6.0" --start

# Custom working directory
python3 arc.py --kubeconfig ~/.kube/config --working-directory "/tmp/retis" --start

# Parallel execution on multiple nodes
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker*" --parallel --start

# Explicit dry run (redundant with default, but clear)
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker*" --dry-run

# Custom RETIS command (full control)
python3 arc.py --kubeconfig ~/.kube/config --retis-command "collect -o custom.json --max-events 5000" --start

# RETIS profile command (different from collect)
python3 arc.py --kubeconfig ~/.kube/config --retis-command "profile -o profile.json -t 30" --start
```

### Utility Operations (Execute Immediately)

```bash
# Stop RETIS collection (executes immediately, no --start needed)
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker*" --stop

# Stop with parallel execution
python3 arc.py --kubeconfig ~/.kube/config --stop --parallel

# Reset failed RETIS units
python3 arc.py --kubeconfig ~/.kube/config --reset-failed

# Download all events.json files from nodes
python3 arc.py --kubeconfig ~/.kube/config --download-results

# Preview utility operations (use --dry-run)
python3 arc.py --kubeconfig ~/.kube/config --stop --dry-run
python3 arc.py --kubeconfig ~/.kube/config --download-results --dry-run
```

## 📖 Command Line Arguments

### Core Options
| Argument | Short | Description | Default |
|----------|--------|-------------|---------|
| `--kubeconfig` | `-k` | Path to kubeconfig file | Prompts if not provided |
| `--node-filter` | `-n` | Glob pattern to filter nodes by name (`worker-2*`) | None |
| `--workload-filter` | `-w` | Regex pattern to filter nodes by workload | None |
| `--dry-run` | | Show commands without executing (default for collection) | Collection: True, Utils: False |
| `--start` | | Actually execute RETIS collection (overrides dry-run) | False |
| `--parallel` | | Run on all nodes in parallel | False (sequential) |

### RETIS Configuration
| Argument | Short | Description | Default |
|----------|--------|-------------|---------|
| `--retis-image` | | RETIS container image to use | `image-registry.openshift-image-registry.svc:5000/default/retis` |
| `--retis-tag` | | RETIS version tag to use | `v1.5.2` |
| `--retis-command` | | Complete RETIS command string (overrides other options) | None |
| `--working-directory` | | Working directory for RETIS collection | `/var/tmp` |
| `--output-file` | `-o` | Output file name for RETIS collection | `events.json` |
| `--filter-packet` | | Packet filter expression | `tcp port 8080 or tcp port 8081` |
| `--retis-extra-args` | | Additional arguments for retis collect command | None |

### RETIS Flags (Enabled by Default)
| Argument | Description | Default |
|----------|-------------|---------|
| `--allow-system-changes` / `--no-allow-system-changes` | Allow/disallow system changes | Enabled |
| `--ovs-track` / `--no-ovs-track` | Enable/disable OVS tracking | Enabled |
| `--stack` / `--no-stack` | Enable/disable stack trace collection | Enabled |
| `--probe-stack` / `--no-probe-stack` | Enable/disable probe stack collection | Enabled |

### Operations
| Argument | Description | Execution |
|----------|-------------|-----------|
| `--stop` | Stop RETIS collection on filtered nodes | Immediate |
| `--reset-failed` | Reset failed RETIS systemd units | Immediate |
| `--download-results` | Download events.json files from nodes | Immediate |

## 🔍 Filtering Options

### Node Name Filtering (`--node-filter`)

Filter nodes using **glob patterns** (shell-style wildcards) on node names:

```bash
# Match nodes starting with "worker-2"
--node-filter "worker-2*"

# Match all worker nodes
--node-filter "worker*"

# Match specific worker numbers
--node-filter "worker-[12]*"

# Match compute nodes
--node-filter "*compute*"
```

**Glob Pattern Syntax:**
- `*` - Matches any characters
- `?` - Matches any single character  
- `[abc]` - Matches any character in brackets
- `[a-z]` - Matches any character in range

### Workload Filtering (`--workload-filter`)

Filter nodes based on workloads (pods) running on them:

```bash
# Nodes running OVN networking components
--workload-filter "ovn"

# Nodes running nginx workloads
--workload-filter "nginx"

# Nodes in specific namespaces
--workload-filter "kube-system"

# Complex patterns
--workload-filter "app=frontend"
```

The workload filter searches through:
- Pod names
- Pod namespaces  
- Pod labels (as `key=value` pairs)

## 🛡️ Safety Features

### Interactive Confirmation
When no filters are specified, the script will:
1. Warn that it will run on ALL worker nodes
2. Prompt for confirmation (unless in dry-run mode)
3. Allow cancellation

### Dry Run Mode
Use `--dry-run` to:
- See which nodes would be selected
- Preview commands that would be executed
- Test filters without making changes

## 📝 Examples

### Example 1: Target Specific Worker Nodes
```bash
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker-0[1-3]"
```

### Example 2: Find Nodes Running OVN Components
```bash
python3 arc.py --kubeconfig ~/.kube/config --workload-filter "ovn-kubernetes"
```

### Example 3: Combined Filtering with Parallel Execution
```bash
python3 arc.py \
  --kubeconfig ~/.kube/config \
  --node-filter "compute" \
  --workload-filter "networking" \
  --parallel \
  --dry-run
```

### Example 4: Stop RETIS on All Worker Nodes
```bash
python3 arc.py --kubeconfig ~/.kube/config --stop --parallel
```

### Example 5: Download Results from Specific Nodes
```bash
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker-2*" --download-results
```

### Example 6: Custom RETIS Command
```bash
python3 arc.py --kubeconfig ~/.kube/config --retis-command "profile -o network-profile.json -t 60" --start
```

## 🚀 Major Features

### 🛡️ Safe-by-Default Behavior

**RETIS Collection Operations** default to **dry-run mode** for safety:
```bash
# Safe: Previews what would be executed
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker*"

# Explicit: Actually executes collection
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker*" --start
```

**Utility Operations** execute immediately as expected:
```bash
# These execute immediately (no --start needed)
python3 arc.py --kubeconfig ~/.kube/config --stop
python3 arc.py --kubeconfig ~/.kube/config --reset-failed  
python3 arc.py --kubeconfig ~/.kube/config --download-results

# Use --dry-run to preview utility operations
python3 arc.py --kubeconfig ~/.kube/config --stop --dry-run
```

### 📥 Automated Results Download

Download all `events.json` files from filtered nodes with automatic naming:

```bash
# Download from all nodes
python3 arc.py --kubeconfig ~/.kube/config --download-results

# Download from specific nodes only
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker-2*" --download-results

# Custom output file name
python3 arc.py --kubeconfig ~/.kube/config --output-file "trace.json" --download-results
```

**File Naming**: Files are saved as `{node-short-name}_{output-file}` to prevent overwrites:
- `worker-0_events.json`
- `worker-1_events.json` 
- `worker-2_events.json`

### 🔧 Custom RETIS Commands

Take full control over RETIS execution with `--retis-command`:

```bash
# Custom collect command
python3 arc.py --kubeconfig ~/.kube/config --retis-command "collect -o custom.json --max-events 5000" --start

# RETIS profile instead of collect
python3 arc.py --kubeconfig ~/.kube/config --retis-command "profile -o profile.json -t 30" --start

# Complex command with multiple options
python3 arc.py --kubeconfig ~/.kube/config --retis-command "collect -o trace.json --allow-system-changes --filter-packet 'tcp port 443' --max-events 10000" --start
```

**Override Behavior**: When using `--retis-command`, individual RETIS parameters are ignored with warnings.

### 🏷️ Version Control

Control RETIS version with `--retis-tag`:

```bash
# Use specific version (default: v1.5.2)
python3 arc.py --kubeconfig ~/.kube/config --retis-tag "v1.6.0" --start

# Use latest version
python3 arc.py --kubeconfig ~/.kube/config --retis-tag "latest" --start

# Use development version
python3 arc.py --kubeconfig ~/.kube/config --retis-tag "main" --start
```

### 🔄 System Maintenance

Reset failed systemd units across nodes:

```bash
# Reset failed units on all nodes
python3 arc.py --kubeconfig ~/.kube/config --reset-failed

# Reset on specific nodes
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker*" --reset-failed

# Preview reset operation
python3 arc.py --kubeconfig ~/.kube/config --reset-failed --dry-run
```

## 🔧 Technical Details

### Node Selection Logic
1. **Worker Node Detection**: Automatically excludes master/control-plane nodes
2. **Name Filtering**: Applied first using glob pattern matching (`fnmatch`)
3. **Workload Filtering**: Applied second by checking pods on remaining nodes
4. **Final Validation**: Ensures at least one node matches before proceeding

### RETIS Collection Process
1. **Script Download**: Downloads `retis_in_container.sh` to local temp file
2. **Node Setup**: Copies script to each target node's working directory
3. **Execution**: Runs RETIS using systemd-run for proper process management
4. **Monitoring**: Checks systemd unit status for success/failure
5. **Cleanup**: Removes temporary files

### Error Handling
- **Kubernetes API Errors**: Graceful handling of connection and permission issues
- **Node Access Errors**: Individual node failures don't stop other nodes
- **Timeout Protection**: All operations have reasonable timeout limits
- **Resource Cleanup**: Temporary files are always cleaned up

## 🚨 Troubleshooting

### Common Issues

**"No module named 'kubernetes'"**
```bash
pip install "kubernetes>=18.20.0"
```

**"Failed to connect to Kubernetes cluster"**
- Verify kubeconfig file exists and is valid
- Check cluster connectivity
- Ensure proper RBAC permissions

**"No nodes found matching filters"**
- Verify glob patterns are correct (use `*` for wildcards, not regex)
- Check that target nodes exist and are workers
- Use dry-run mode to test filters (default for collection operations)

**"Warning: Individual RETIS parameters are ignored when using --retis-command"**
- This is expected when using `--retis-command` with other RETIS options
- The custom command takes full precedence

**"No results files found for download"**
- Ensure RETIS collection has completed successfully 
- Check that the output file exists on target nodes
- Verify working directory and file names

**"oc command not found" (shouldn't occur with current version)**
- This script now uses Kubernetes API directly
- No need for `oc` CLI tool

### Debug Tips

1. **Default Preview Mode**: Collection operations preview by default (no `--dry-run` needed)
2. **Test Filters**: Use glob patterns like `worker-2*` instead of regex `^worker-2`
3. **Start Small**: Test with a single node first: `--node-filter "worker-0*"`
4. **Use Start Flag**: Remember to add `--start` to actually execute collection
5. **Check Connectivity**: Ensure kubeconfig and cluster access work
6. **Verify Permissions**: Ensure RBAC permissions for nodes and pods access

## 🔄 Recent Changes

### 🚀 Version 2.0 - Major Feature Updates

#### ✨ New Features Added:
- **🛡️ Safe-by-Default**: RETIS collection now defaults to dry-run mode, requires `--start` to execute
- **📥 Results Download**: New `--download-results` operation to fetch all events.json files
- **🔧 Custom Commands**: `--retis-command` option for full control over RETIS arguments
- **🏷️ Version Control**: `--retis-tag` option to specify RETIS version (default: v1.5.2)
- **🔄 System Maintenance**: `--reset-failed` operation to reset failed systemd units
- **🔍 Fixed Node Filtering**: Now uses glob patterns (`worker-2*`) instead of regex for intuitive matching

#### 🛡️ Enhanced Safety:
- **Default Dry-Run**: Collection operations preview by default, utility operations execute immediately
- **Smart Behavior**: Stop, reset-failed, and download operations work without `--start` flag
- **Clear Warnings**: Alerts when conflicting options are used together

#### ⚡ Improved User Experience:
- **Intuitive Filtering**: Glob patterns work like shell wildcards (`worker-2*`, `worker*`)
- **Automatic File Naming**: Downloaded files use node names to prevent overwrites
- **Comprehensive Help**: Updated examples and documentation for all features

### 🔄 Previous Updates - Kubernetes API Migration:
- **No CLI Dependencies**: Removed dependency on `oc` command
- **Better Error Handling**: Native Kubernetes API exceptions
- **Improved Performance**: Direct API calls instead of subprocess overhead
- **Enhanced Reliability**: No CLI output parsing required
- **Type Safety**: Direct object access instead of JSON parsing

### 📈 Backward Compatibility:
- All existing functionality preserved
- Same core command-line interface
- Enhanced with new optional features
- Improved safety with default dry-run mode

## 📄 License

This project is provided as-is for educational and operational purposes.

## 🤝 Contributing

Contributions, issues, and feature requests are welcome!

## 📞 Support

For issues related to:
- **RETIS**: See [RETIS documentation](https://github.com/retis-org/retis)
- **OpenShift**: Consult OpenShift documentation
- **Kubernetes API**: Check Kubernetes Python client documentation
