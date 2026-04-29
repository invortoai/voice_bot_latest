terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "ap-south-1"
}

variable "project_name" {
  description = "Project name for tagging"
  type        = string
  default     = "invorto-ai"
}

variable "vpc_cidr" {
  description = "VPC CIDR block"
  type        = string
  default     = "10.0.0.0/16"
}

variable "runner_instance_type" {
  description = "Instance type for bot runner"
  type        = string
  default     = "t4g.small"
}

variable "worker_instance_type" {
  description = "Instance type for bot workers"
  type        = string
  default     = "t4g.small"
}

variable "worker_count" {
  description = "Number of warm workers to maintain"
  type        = number
  default     = 2
}

variable "worker_ami_id" {
  description = "AMI ID for worker instances (leave empty to use latest Amazon Linux 2023)"
  type        = string
  default     = ""
}

variable "ssh_key_name" {
  description = "SSH key pair name for EC2 instances"
  type        = string
}

variable "admin_cidr_blocks" {
  description = "CIDR blocks allowed SSH access to EC2 instances (e.g. bastion/VPN)"
  type        = list(string)
  default     = []
}

variable "domain_name" {
  description = "Domain name for the bot runner (optional)"
  type        = string
  default     = ""
}

variable "runner_ecr_repo_name" {
  description = "ECR repository name for runner image"
  type        = string
  default     = ""
}

variable "worker_ecr_repo_name" {
  description = "ECR repository name for worker image"
  type        = string
  default     = ""
}

variable "runner_image_tag" {
  description = "Runner image tag to deploy from ECR"
  type        = string
  default     = "latest"
}

variable "worker_image_tag" {
  description = "Worker image tag to deploy from ECR"
  type        = string
  default     = "latest"
}

variable "runner_env_ssm_parameter_name" {
  description = "Optional SSM Parameter Store name containing runner env-file contents (use SecureString). If empty, no env file is loaded."
  type        = string
  default     = ""
}

variable "worker_env_ssm_parameter_name" {
  description = "Optional SSM Parameter Store name containing worker env-file contents (use SecureString). If empty, no env file is loaded."
  type        = string
  default     = ""
}

variable "deploy_runner_from_ecr" {
  description = "If true, runner instance will pull the Docker image from ECR and run it on boot."
  type        = bool
  default     = false
}

variable "deploy_workers_from_ecr" {
  description = "If true, worker instances will pull the Docker image from ECR and run it on boot."
  type        = bool
  default     = false
}

locals {
  runner_repo_name = var.runner_ecr_repo_name != "" ? var.runner_ecr_repo_name : "${var.project_name}-runner"
  worker_repo_name = var.worker_ecr_repo_name != "" ? var.worker_ecr_repo_name : "${var.project_name}-worker"
  ecr_registry     = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
}

resource "aws_cloudwatch_log_group" "runner" {
  name              = "/aws/ec2/${var.project_name}-runner"
  retention_in_days = 7

  tags = {
    Project = var.project_name
  }
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/ec2/${var.project_name}-worker"
  retention_in_days = 7

  tags = {
    Project = var.project_name
  }
}

resource "aws_ecr_repository" "runner" {
  name                 = local.runner_repo_name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Project = var.project_name
  }
}

resource "aws_ecr_repository" "worker" {
  name                 = local.worker_repo_name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = {
    Project = var.project_name
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_ami" "amazon_linux_2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-*-arm64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name    = "${var.project_name}-vpc"
    Project = var.project_name
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name    = "${var.project_name}-igw"
    Project = var.project_name
  }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name    = "${var.project_name}-public-${count.index + 1}"
    Project = var.project_name
    Type    = "public"
  }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = {
    Name    = "${var.project_name}-private-${count.index + 1}"
    Project = var.project_name
    Type    = "private"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name    = "${var.project_name}-public-rt"
    Project = var.project_name
  }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  domain = "vpc"

  tags = {
    Name    = "${var.project_name}-nat-eip"
    Project = var.project_name
  }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id

  tags = {
    Name    = "${var.project_name}-nat"
    Project = var.project_name
  }

  depends_on = [aws_internet_gateway.main]
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }

  tags = {
    Name    = "${var.project_name}-private-rt"
    Project = var.project_name
  }
}

resource "aws_route_table_association" "private" {
  count          = length(aws_subnet.private)
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

resource "aws_security_group" "runner" {
  name        = "${var.project_name}-runner-sg"
  description = "Security group for bot runner"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 7860
    to_port         = 7860
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  dynamic "ingress" {
    for_each = length(var.admin_cidr_blocks) > 0 ? [1] : []
    content {
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = var.admin_cidr_blocks
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.project_name}-runner-sg"
    Project = var.project_name
  }
}

resource "aws_security_group" "worker" {
  name        = "${var.project_name}-worker-sg"
  description = "Security group for bot workers"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 8765
    to_port         = 8765
    protocol        = "tcp"
    security_groups = [aws_security_group.runner.id]
  }

  # NOTE: Removed 0.0.0.0/0 rule on port 8765 — runner-SG rule above
  # already allows runner-to-worker communication on this port.

  # Public TLS termination for Twilio Media Streams (wss://...)
  # If you use per-instance TLS termination (e.g., Caddy + Let's Encrypt),
  # Twilio will connect to 443 and optionally 80 is needed for ACME HTTP-01.
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  dynamic "ingress" {
    for_each = length(var.admin_cidr_blocks) > 0 ? [1] : []
    content {
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = var.admin_cidr_blocks
    }
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.project_name}-worker-sg"
    Project = var.project_name
  }
}

resource "aws_security_group" "alb" {
  name        = "${var.project_name}-alb-sg"
  description = "Security group for ALB"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "${var.project_name}-alb-sg"
    Project = var.project_name
  }
}

resource "aws_iam_role" "runner" {
  name = "${var.project_name}-runner-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Project = var.project_name
  }
}

resource "aws_iam_role_policy" "runner_policy" {
  name = "${var.project_name}-runner-policy"
  role = aws_iam_role.runner.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeTags"
        ]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = "${aws_cloudwatch_log_group.runner.arn}:*"
      }
    ]
  })
}

resource "aws_iam_role_policy" "runner_ecr_pull" {
  name = "${var.project_name}-runner-ecr-pull"
  role = aws_iam_role.runner.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer"
        ]
        Resource = aws_ecr_repository.runner.arn
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "runner" {
  name = "${var.project_name}-runner-profile"
  role = aws_iam_role.runner.name
}

resource "aws_iam_role" "worker" {
  name = "${var.project_name}-worker-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Project = var.project_name
  }
}

resource "aws_iam_role_policy" "worker_ecr_pull" {
  name = "${var.project_name}-worker-ecr-pull"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer"
        ]
        Resource = aws_ecr_repository.worker.arn
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = "${aws_cloudwatch_log_group.worker.arn}:*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "worker" {
  name = "${var.project_name}-worker-profile"
  role = aws_iam_role.worker.name
}

resource "aws_lb" "runner" {
  name               = "${var.project_name}-runner-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  tags = {
    Name    = "${var.project_name}-runner-alb"
    Project = var.project_name
  }
}

resource "aws_lb_target_group" "runner" {
  name     = "${var.project_name}-runner-tg"
  port     = 7860
  protocol = "HTTP"
  vpc_id   = aws_vpc.main.id

  health_check {
    enabled             = true
    healthy_threshold   = 2
    # ALB health check interval minimum is 5s
    interval            = 5
    matcher             = "200"
    path                = "/health"
    port                = "traffic-port"
    protocol            = "HTTP"
    timeout             = 2
    unhealthy_threshold = 2
  }

  tags = {
    Project = var.project_name
  }
}

resource "aws_lb_listener" "runner_http" {
  load_balancer_arn = aws_lb.runner.arn
  port              = "80"
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.runner.arn
  }
}

resource "aws_instance" "runner" {
  ami                    = data.aws_ami.amazon_linux_2023.id
  instance_type          = var.runner_instance_type
  key_name               = var.ssh_key_name
  vpc_security_group_ids = [aws_security_group.runner.id]
  subnet_id              = aws_subnet.public[0].id
  iam_instance_profile   = aws_iam_instance_profile.runner.name

  # NOTE: Keep the shebang at column 0. Terraform heredoc `<<-EOF` strips leading TABS only
  # (not spaces), and cloud-init/runparts requires a valid shebang for direct execution.
  user_data = base64encode(<<-EOF
#!/bin/bash
set -e

dnf install -y docker awscli
systemctl start docker
systemctl enable docker

mkdir -p /opt/invorto-ai

if [ "${var.deploy_runner_from_ecr}" = "true" ]; then
  REGISTRY="${local.ecr_registry}"
  IMAGE="${local.ecr_registry}/${aws_ecr_repository.runner.name}:${var.runner_image_tag}"

  aws ecr get-login-password --region "${var.aws_region}" | docker login --username AWS --password-stdin "$REGISTRY"

  ENV_FILE="/opt/invorto-ai/runner.env"
  if [ -n "${var.runner_env_ssm_parameter_name}" ]; then
    aws ssm get-parameter --with-decryption --name "${var.runner_env_ssm_parameter_name}" --region "${var.aws_region}" --query "Parameter.Value" --output text > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
  else
    touch "$ENV_FILE"
  fi

  docker pull "$IMAGE"
  docker rm -f invorto-runner || true

  docker run -d --restart=always --name invorto-runner \
    -p 7860:7860 \
    --env-file "$ENV_FILE" \
    -e AWS_REGION="${var.aws_region}" \
    -e PROJECT_NAME="${var.project_name}" \
    -e PORT="7860" \
    -e ENVIRONMENT="production" \
    -e WORKER_PUBLIC_WS_SCHEME="wss" \
    -e WORKER_PUBLIC_WS_PORT="443" \
    -e WORKER_PUBLIC_WS_HOST_SUFFIX=".sslip.io" \
    "$IMAGE"
fi

echo "Bot runner instance initialized"
EOF
  )

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
  }

  tags = {
    Name    = "${var.project_name}-runner"
    Project = var.project_name
    Role    = "runner"
  }
}

resource "aws_lb_target_group_attachment" "runner" {
  target_group_arn = aws_lb_target_group.runner.arn
  target_id        = aws_instance.runner.id
  port             = 7860
}

resource "aws_instance" "worker" {
  count                  = var.worker_count
  ami                    = var.worker_ami_id != "" ? var.worker_ami_id : data.aws_ami.amazon_linux_2023.id
  instance_type          = var.worker_instance_type
  key_name               = var.ssh_key_name
  vpc_security_group_ids = [aws_security_group.worker.id]
  subnet_id              = aws_subnet.public[count.index % length(aws_subnet.public)].id
  iam_instance_profile   = aws_iam_instance_profile.worker.name

  # NOTE: Keep the shebang at column 0. Terraform heredoc `<<-EOF` strips leading TABS only
  # (not spaces), and cloud-init/runparts requires a valid shebang for direct execution.
  user_data = base64encode(<<-EOF
#!/bin/bash
set -euo pipefail

# Install dependencies
dnf install -y docker awscli
systemctl enable --now docker

# =============================================================================
# CADDY INSTALLATION
# TLS termination for Twilio Media Streams (wss://...) using per-instance hostname.
# We use Caddy to obtain a publicly-trusted cert for "<public-ip>.sslip.io"
# and reverse proxy :443 -> localhost:8765 (the worker container).
# =============================================================================

# Install Caddy from GitHub releases (Cloudsmith repo is deprecated/404)
CADDY_VERSION="2.8.4"
curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v$${CADDY_VERSION}/caddy_$${CADDY_VERSION}_linux_arm64.tar.gz" -o /tmp/caddy.tar.gz
tar -xzf /tmp/caddy.tar.gz -C /usr/local/bin caddy
chmod +x /usr/local/bin/caddy
rm -f /tmp/caddy.tar.gz

# Create caddy user and directories
useradd --system --home /var/lib/caddy --shell /usr/sbin/nologin caddy || true
mkdir -p /etc/caddy /var/lib/caddy /var/log/caddy
chown caddy:caddy /var/lib/caddy /var/log/caddy

# Create systemd service for Caddy
cat > /etc/systemd/system/caddy.service << 'CADDYUNITEOF'
[Unit]
Description=Caddy
Documentation=https://caddyserver.com/docs/
After=network.target network-online.target
Requires=network-online.target

[Service]
Type=notify
User=caddy
Group=caddy
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576
LimitNPROC=512
PrivateTmp=true
ProtectSystem=full
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
CADDYUNITEOF

systemctl daemon-reload
systemctl enable caddy

# =============================================================================
# CONFIGURE CADDY SCRIPT
# This script fetches the public IP and configures Caddy with the sslip.io hostname
# =============================================================================

cat > /usr/local/bin/configure-caddy-worker.sh << 'CADDYSETEOF'
#!/bin/bash
set -euo pipefail

# Fetch public IPv4 (IMDSv2)
TOKEN="$(curl -fsS -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")"
PUBLIC_IP="$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" "http://169.254.169.254/latest/meta-data/public-ipv4")"

HOST="$PUBLIC_IP.sslip.io"

# Create Caddyfile with proper TLS termination
cat > /etc/caddy/Caddyfile <<CADDYFILEEOF
$HOST {
  reverse_proxy 127.0.0.1:8765
}
CADDYFILEEOF
chown caddy:caddy /etc/caddy/Caddyfile

# Start or reload Caddy
if systemctl is-active --quiet caddy; then
  /usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force
else
  systemctl start caddy
fi
CADDYSETEOF
chmod +x /usr/local/bin/configure-caddy-worker.sh

cat > /etc/systemd/system/configure-caddy-worker.service << 'CADDYSVC'
[Unit]
Description=Configure Caddy for Invorto worker (per-instance hostname)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/configure-caddy-worker.sh
RemainAfterExit=true

[Install]
WantedBy=multi-user.target
CADDYSVC

systemctl daemon-reload
systemctl enable configure-caddy-worker.service

# Run the configure script directly (creates Caddyfile and starts Caddy)
/usr/local/bin/configure-caddy-worker.sh

# =============================================================================
# DOCKER CONTAINER DEPLOYMENT
# =============================================================================

mkdir -p /opt/invorto-ai

if [ "${var.deploy_workers_from_ecr}" = "true" ]; then
  REGISTRY="${local.ecr_registry}"
  IMAGE="${local.ecr_registry}/${aws_ecr_repository.worker.name}:${var.worker_image_tag}"

  aws ecr get-login-password --region "${var.aws_region}" | docker login --username AWS --password-stdin "$REGISTRY"

  ENV_FILE="/opt/invorto-ai/worker.env"
  if [ -n "${var.worker_env_ssm_parameter_name}" ]; then
    aws ssm get-parameter --with-decryption --name "${var.worker_env_ssm_parameter_name}" --region "${var.aws_region}" --query "Parameter.Value" --output text > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
  else
    touch "$ENV_FILE"
  fi

  docker pull "$IMAGE"
  docker rm -f "invorto-worker-$(hostname)" || true

  docker run -d --restart=always --name "invorto-worker-$(hostname)" \
    -p 8765:8765 \
    --env-file "$ENV_FILE" \
    -e AWS_REGION="${var.aws_region}" \
    -e PROJECT_NAME="${var.project_name}" \
    -e ENVIRONMENT="production" \
    "$IMAGE"
fi

echo "Bot worker instance initialized successfully"
EOF
  )

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
  }

  tags = {
    Name    = "${var.project_name}-worker-${count.index + 1}"
    Project = var.project_name
    Role    = "worker"
    Type    = "invorto-ai-worker"
  }
}

output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.main.id
}

output "runner_public_ip" {
  description = "Bot runner public IP"
  value       = aws_instance.runner.public_ip
}

output "runner_alb_dns" {
  description = "Bot runner ALB DNS name"
  value       = aws_lb.runner.dns_name
}

output "worker_public_ips" {
  description = "Bot worker public IPs"
  value       = aws_instance.worker[*].public_ip
}

output "worker_public_ws_urls" {
  description = "Public wss:// URLs Jambonz should connect to (per-instance sslip.io hostname, TLS on 443)"
  value       = [for ip in aws_instance.worker[*].public_ip : "wss://${ip}.sslip.io/ws/jambonz"]
}

output "worker_private_ips" {
  description = "Bot worker private IPs"
  value       = aws_instance.worker[*].private_ip
}

output "jambonz_webhook_url" {
  description = "URL to configure in Jambonz for incoming calls"
  value       = "http://${aws_lb.runner.dns_name}/jambonz/incoming"
}

output "jambonz_status_callback_url" {
  description = "URL for Jambonz call status callbacks"
  value       = "http://${aws_lb.runner.dns_name}/jambonz/status"
}

output "runner_ecr_repository_url" {
  description = "ECR repository URL for the runner image"
  value       = aws_ecr_repository.runner.repository_url
}

output "worker_ecr_repository_url" {
  description = "ECR repository URL for the worker image"
  value       = aws_ecr_repository.worker.repository_url
}
