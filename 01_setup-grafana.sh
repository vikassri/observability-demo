#!/bin/bash
set -e

echo "========================================"
echo "  Grafana Setup on Mail Server"
echo "========================================"

SSH_KEY="/Users/vikassri/Desktop/uob_poc/mail-server.pem"
HOST="ec2-user@observability.spoc.cloudera-labs.com"
SSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=no $HOST"

echo ">>> Testing connection..."
$SSH hostnamectl

echo ">>> Installing Grafana via direct RPM..."
$SSH bash << 'REMOTE'
    set -e

    # Check Amazon Linux version for correct repo
    . /etc/os-release
    echo "OS: $ID $VERSION_ID"

    # Install dependencies
    sudo dnf install -y wget

    # Download and install Grafana Enterprise RPM directly
    wget -q https://dl.grafana.com/enterprise/release/grafana-enterprise-11.3.0-1.x86_64.rpm -O /tmp/grafana.rpm

    sudo dnf install -y /tmp/grafana.rpm

    # Start and enable
    sudo systemctl daemon-reload
    sudo systemctl enable grafana-server
    sudo systemctl start grafana-server

    # Cleanup
    rm -f /tmp/grafana.rpm
REMOTE

echo ">>> Grafana status:"
$SSH sudo systemctl status grafana-server --no-pager | head -25

echo ""
echo "========================================"
echo "  Done! Access: http://observability.spoc.cloudera-labs.com:3000"
echo "  Login: admin / admin"
echo "========================================"
