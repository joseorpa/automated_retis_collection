# Automated RETIS Collection (ARC)

A Python script for running RETIS network packet collection on OpenShift/Kubernetes worker nodes with advanced filtering capabilities.

## 🎯 Overview

This tool automates the deployment and execution of RETIS (Real-time Traffic Inspection System) on Kubernetes worker nodes. It provides flexible node selection through name patterns and workload filtering, making it easy to target specific nodes for network analysis.

## ✨ Features

- **🔍 Smart Node Filtering**: Filter nodes by name patterns or running workloads
- **🚀 Parallel Execution**: Run RETIS collection on multiple nodes simultaneously
- **⚙️ Flexible Configuration**: Support for custom RETIS images and working directories
- **🛡️ Safety Features**: Dry-run mode and interactive confirmation for bulk operations
- **📋 Comprehensive Management**: Start, stop, and monitor RETIS collection processes
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

2. **Install dependencies:**
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
# Filter nodes by name pattern
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker.*"

# Filter nodes running specific workloads
python3 arc.py --kubeconfig ~/.kube/config --workload-filter "ovn"

# Combine both filters
python3 arc.py --kubeconfig ~/.kube/config --node-filter "compute" --workload-filter "networking"

# Use without kubeconfig argument (will prompt)
python3 arc.py --node-filter "worker.*"
```

### Advanced Usage

```bash
# Custom RETIS image
python3 arc.py --kubeconfig ~/.kube/config --retis-image "registry.example.com/retis:custom"

# Custom working directory
python3 arc.py --kubeconfig ~/.kube/config --working-directory "/tmp/retis"

# Parallel execution on multiple nodes
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker" --parallel

# Dry run to see what would be executed
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker" --dry-run

# Stop RETIS collection
python3 arc.py --kubeconfig ~/.kube/config --node-filter "worker" --stop

# Stop with parallel execution
python3 arc.py --kubeconfig ~/.kube/config --stop --parallel
```

## 📖 Command Line Arguments

| Argument | Short | Description | Default |
|----------|--------|-------------|---------|
| `--kubeconfig` | `-k` | Path to kubeconfig file | Prompts if not provided |
| `--node-filter` | `-n` | Regex pattern to filter nodes by name | None |
| `--workload-filter` | `-w` | Regex pattern to filter nodes by workload | None |
| `--retis-image` | | RETIS container image to use | `image-registry.openshift-image-registry.svc:5000/default/retis` |
| `--working-directory` | | Working directory for RETIS collection | `/var/tmp` |
| `--dry-run` | | Show commands without executing | False |
| `--parallel` | | Run on all nodes in parallel | False (sequential) |
| `--stop` | | Stop RETIS collection on filtered nodes | False |

## 🔍 Filtering Options

### Node Name Filtering (`--node-filter`)

Filter nodes using regular expressions on node names:

```bash
# Match nodes starting with "worker"
--node-filter "^worker"

# Match nodes containing "compute"
--node-filter "compute"

# Match specific node patterns
--node-filter "worker-[0-9]+"
```

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

## 🔧 Technical Details

### Node Selection Logic
1. **Worker Node Detection**: Automatically excludes master/control-plane nodes
2. **Name Filtering**: Applied first using regex matching
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
- Verify filter patterns are correct
- Check that target nodes exist and are workers
- Use `--dry-run` to test filters

**"oc command not found" (shouldn't occur with current version)**
- This script now uses Kubernetes API directly
- No need for `oc` CLI tool

### Debug Tips

1. **Use dry-run mode first**: `--dry-run` to preview operations
2. **Start with broad filters**: Test with simple patterns first
3. **Check node labels**: Verify worker node detection logic
4. **Test connectivity**: Ensure kubeconfig and cluster access work

## 🔄 Recent Changes

This version has been updated to use the **Kubernetes Python client API** instead of CLI commands:

### ✅ Improvements Made:
- **No CLI Dependencies**: Removed dependency on `oc` command
- **Better Error Handling**: Native Kubernetes API exceptions
- **Improved Performance**: Direct API calls instead of subprocess overhead
- **Enhanced Reliability**: No CLI output parsing required
- **Type Safety**: Direct object access instead of JSON parsing

### 🔄 Migration from CLI Version:
- All functionality preserved
- Same command-line interface
- Same filtering capabilities
- Added kubeconfig parameter requirement
- Enhanced error messages and feedback

## 📄 License

This project is provided as-is for educational and operational purposes.

## 🤝 Contributing

Contributions, issues, and feature requests are welcome!

## 📞 Support

For issues related to:
- **RETIS**: See [RETIS documentation](https://github.com/retis-org/retis)
- **OpenShift**: Consult OpenShift documentation
- **Kubernetes API**: Check Kubernetes Python client documentation
