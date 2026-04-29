# Invorto AI - Production Deployment Guide

Complete guide for deploying the Invorto AI Voice Bot Platform to AWS production environment.

## Table of Contents
- [Deployment Architecture](#deployment-architecture)
- [Prerequisites](#prerequisites)
- [AWS Infrastructure Setup](#aws-infrastructure-setup)
- [Database Setup](#database-setup)
- [Environment Configuration](#environment-configuration)
- [Docker Image Build & Push](#docker-image-build--push)
- [Terraform Deployment](#terraform-deployment)
- [Post-Deployment Configuration](#post-deployment-configuration)
- [Monitoring & Logging](#monitoring--logging)
- [Scaling Strategy](#scaling-strategy)
- [Security Best Practices](#security-best-practices)
- [CI/CD Pipeline](#cicd-pipeline)
- [Maintenance & Updates](#maintenance--updates)
- [Troubleshooting](#troubleshooting)

---

## Deployment Architecture

### High-Level Overview

```
┌─────────────────────────────────────────────────────────────┐
│                         AWS Cloud                            │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌────────────────┐        ┌──────────────────────┐         │
│  │   Internet     │───────▶│  Application Load    │         │
│  │   Gateway      │        │  Balancer (ALB)      │         │
│  └────────────────┘        └──────────────────────┘         │
│                                      │                        │
│                                      ▼                        │
│                           ┌─────────────────┐                │
│                           │   Bot Runner    │                │
│                           │   EC2 Instance  │                │
│                           │   (t3.small)    │                │
│                           │   Port 7860     │                │
│                           └─────────────────┘                │
│                                      │                        │
│                    ┌─────────────────┼─────────────────┐     │
│                    ▼                 ▼                 ▼     │
│             ┌───────────┐     ┌───────────┐     ┌───────────┐│
│             │  Worker 1 │     │  Worker 2 │     │  Worker N ││
│             │    EC2    │     │    EC2    │     │    EC2    ││
│             │ (t3.small)│     │ (t3.small)│     │ (t3.small)││
│             │  Port 443 │     │  Port 443 │     │  Port 443 ││
│             │ (wss://)  │     │ (wss://)  │     │ (wss://)  ││
│             └───────────┘     └───────────┘     └───────────┘│
│                                                               │
│  ┌──────────────────────────────────────────────────────┐   │
│  │             Amazon CloudWatch Logs                    │   │
│  │  - /aws/ec2/invorto-ai-runner (7 day retention)      │   │
│  │  - /aws/ec2/invorto-ai-worker (7 day retention)      │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                               │
│  ┌──────────────────────────────────────────────────────┐   │
│  │     Amazon ECR (Container Registry)                   │   │
│  │  - invorto-ai-runner:latest                           │   │
│  │  - invorto-ai-worker:latest                           │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                               │
│  ┌──────────────────────────────────────────────────────┐   │
│  │     AWS Systems Manager Parameter Store              │   │
│  │  - /invorto-ai/runner-env (SecureString)             │   │
│  │  - /invorto-ai/worker-env (SecureString)             │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                               │
└─────────────────────────────────────────────────────────────┘

        ▲                                            ▲
        │                                            │
   ┌────────────┐                              ┌──────────┐
   │  Twilio    │                              │ External │
   │  Webhooks  │                              │ Database │
   │  Jambonz   │                              │ (RDS)    │
   └────────────┘                              └──────────┘
```

### Component Responsibilities

#### Application Load Balancer (ALB)
- Routes external traffic to bot runner
- Health checks on port 7860
- SSL/TLS termination (optional)

#### Bot Runner EC2 Instance
- Runs `invorto-ai-runner` Docker container
- Discovers workers via EC2 tags
- Routes calls to available workers
- Exposes REST API for management
- Logs to CloudWatch

#### Worker EC2 Instances (Auto Scaling Group)
- Runs `invorto-ai-worker` Docker container
- Uses `.sslip.io` for TLS termination on port 443
- Handles WebSocket connections from Twilio
- Processes AI voice conversations
- Auto-scales based on demand

---

## Prerequisites

### Required Accounts & Services

1. **AWS Account**
   - IAM user with admin privileges or appropriate permissions
   - AWS CLI configured with credentials
   - SSH key pair created in target region

2. **Database**
   - PostgreSQL 14+ database (AWS RDS recommended)
   - Accessible from EC2 instances
   - Connection string ready

3. **External APIs**
   - OpenAI API key
   - Deepgram API key
   - ElevenLabs API key and voice ID
   - Twilio account (with phone numbers)
   - Jambonz account (optional)

4. **Local Tools**
   - Terraform >= 1.0
   - AWS CLI >= 2.0
   - Docker with BuildKit support
   - Bash shell

### AWS Permissions Required

Your IAM user/role needs permissions for:
- EC2 (instances, security groups, AMIs)
- VPC (subnets, internet gateways, route tables)
- ELB (Application Load Balancers, target groups)
- ECR (repositories, images)
- CloudWatch (log groups, log streams)
- IAM (roles, policies, instance profiles)
- SSM (Parameter Store)
- Auto Scaling (launch templates, auto scaling groups)

---

## AWS Infrastructure Setup

### 1. Create SSH Key Pair

```bash
# Navigate to AWS Console → EC2 → Key Pairs
# Or via AWS CLI:

aws ec2 create-key-pair \
  --key-name invortoai \
  --region ap-south-1 \
  --query 'KeyMaterial' \
  --output text > ~/.ssh/invortoai.pem

chmod 400 ~/.ssh/invortoai.pem
```

### 2. Create PostgreSQL Database (RDS)

#### Via AWS Console:

1. Go to RDS → Create database
2. Choose PostgreSQL 14+
3. Template: Production or Dev/Test
4. Settings:
   - DB instance identifier: `invorto-ai-db`
   - Master username: `invorto_user`
   - Master password: (secure password)
5. Instance configuration: db.t3.micro or larger
6. Storage: 20 GB SSD (auto-scaling enabled)
7. Connectivity:
   - VPC: Will be connected to Terraform VPC later
   - Public access: No (for security)
   - VPC security group: Create new `invorto-ai-db-sg`
8. Database authentication: Password authentication
9. Additional configuration:
   - Initial database name: `invorto_ai`
   - Backup retention: 7 days
   - Encryption: Enabled
10. Create database

#### Via AWS CLI:

```bash
aws rds create-db-instance \
  --db-instance-identifier invorto-ai-db \
  --db-instance-class db.t3.micro \
  --engine postgres \
  --engine-version 14.7 \
  --master-username invorto_user \
  --master-user-password 'YourSecurePassword123!' \
  --allocated-storage 20 \
  --storage-type gp2 \
  --storage-encrypted \
  --backup-retention-period 7 \
  --vpc-security-group-ids sg-xxxxx \
  --db-subnet-group-name default \
  --no-publicly-accessible \
  --region ap-south-1

# Wait for database to be available
aws rds wait db-instance-available \
  --db-instance-identifier invorto-ai-db \
  --region ap-south-1

# Get endpoint
aws rds describe-db-instances \
  --db-instance-identifier invorto-ai-db \
  --region ap-south-1 \
  --query 'DBInstances[0].Endpoint.Address' \
  --output text
```

#### Connection String Format:

```
postgresql://invorto_user:YourSecurePassword123!@invorto-ai-db.xxxxx.ap-south-1.rds.amazonaws.com:5432/invorto_ai
```

### 3. Run Database Migrations

From your local machine (ensure RDS security group allows your IP):

```bash
# Set DATABASE_URL
export DATABASE_URL="postgresql://invorto_user:password@rds-endpoint:5432/invorto_ai"

# Run migrations
cd migrations
python migrate.py

# Verify
python migrate.py --status
```

---

## Environment Configuration

### 1. Prepare Environment Files

Create production environment configurations:

#### Runner Environment (`runner.env`)

```bash
ENVIRONMENT=production
PORT=7860
API_KEY=your-secure-api-key-here
DATABASE_URL=postgresql://username:password@your-db-endpoint.region.rds.amazonaws.com:5432/database_name
AWS_REGION=ap-south-1
WORKER_POOL_TAG=invorto-ai-worker
WORKER_PORT=8765


JAMBONZ_API_URL=https://jambonz.cloud/api
JAMBONZ_ACCOUNT_SID=your-jambonz-account-sid
JAMBONZ_API_KEY=your-jambonz-api-key
JAMBONZ_APPLICATION_SID=your-jambonz-application-sid
```

#### Worker Environment (`worker.env`)

```bash
ENVIRONMENT=production
WORKER_PORT=8765
DATABASE_URL=postgresql://username:password@your-db-endpoint.region.rds.amazonaws.com:5432/database_name
OPENAI_API_KEY=sk-proj-your-openai-api-key
ELEVENLABS_API_KEY=sk_your-elevenlabs-api-key
ELEVENLABS_VOICE_ID=your-elevenlabs-voice-id
DEEPGRAM_API_KEY=your-deepgram-api-key

JAMBONZ_API_URL=https://jambonz.cloud/api
JAMBONZ_ACCOUNT_SID=your-jambonz-account-sid
JAMBONZ_API_KEY=your-jambonz-api-key
JAMBONZ_APPLICATION_SID=your-jambonz-application-sid
```

### 2. Store Environment Variables in AWS SSM Parameter Store

```bash
# Store runner environment (as SecureString)
aws ssm put-parameter \
  --name "/invorto-ai/runner-env" \
  --type "SecureString" \
  --value "$(cat runner.env)" \
  --region ap-south-1 \
  --overwrite

# Store worker environment (as SecureString)
aws ssm put-parameter \
  --name "/invorto-ai/worker-env" \
  --type "SecureString" \
  --value "$(cat worker.env)" \
  --region ap-south-1 \
  --overwrite

# Verify
aws ssm get-parameter \
  --name "/invorto-ai/runner-env" \
  --region ap-south-1 \
  --with-decryption
```

**Security Note**: Never commit these environment files to Git. Add them to `.gitignore`.

---

## Docker Image Build & Push

### 1. Configure AWS CLI

```bash
# Configure AWS credentials
aws configure

# Or set environment variables
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=ap-south-1
```

### 2. Build and Push Images

#### Method A: Using the Push Script (Recommended)

```bash
# View script help
./scripts/push_to_ecr.sh --help

# Build and push both images
./scripts/push_to_ecr.sh \
  --region ap-south-1 \
  --account-id 123456789012 \
  --runner-repo invorto-ai-runner \
  --worker-repo invorto-ai-worker \
  --tag latest

# Build for specific platform (default: linux/amd64)
./scripts/push_to_ecr.sh \
  --region ap-south-1 \
  --account-id 123456789012 \
  --runner-repo invorto-ai-runner \
  --worker-repo invorto-ai-worker \
  --tag latest \
  --platform linux/amd64

# Build multi-arch images (AMD64 + ARM64)
./scripts/push_to_ecr.sh \
  --region ap-south-1 \
  --account-id 123456789012 \
  --runner-repo invorto-ai-runner \
  --worker-repo invorto-ai-worker \
  --tag latest \
  --multi-arch
```

#### Method B: Using Make (Convenience)

```bash
make ecr-push \
  AWS_REGION=ap-south-1 \
  AWS_ACCOUNT_ID=123456789012 \
  TAG=latest
```

#### Method C: Manual Steps

```bash
# Get AWS account ID
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
AWS_REGION=ap-south-1
REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Create ECR repositories (if not exist)
aws ecr create-repository \
  --repository-name invorto-ai-runner \
  --region $AWS_REGION || true

aws ecr create-repository \
  --repository-name invorto-ai-worker \
  --region $AWS_REGION || true

# Login to ECR
aws ecr get-login-password --region $AWS_REGION | \
  docker login --username AWS --password-stdin $REGISTRY

# Build runner image
docker buildx build \
  --platform linux/amd64 \
  -f Dockerfile.runner \
  -t $REGISTRY/invorto-ai-runner:latest \
  --push .

# Build worker image
docker buildx build \
  --platform linux/amd64 \
  -f Dockerfile.worker \
  -t $REGISTRY/invorto-ai-worker:latest \
  --push .

# Verify images
aws ecr describe-images \
  --repository-name invorto-ai-runner \
  --region $AWS_REGION

aws ecr describe-images \
  --repository-name invorto-ai-worker \
  --region $AWS_REGION
```

### 3. Image Versioning Strategy

```bash
# Use git commit hash for versioning
GIT_HASH=$(git rev-parse --short HEAD)

./scripts/push_to_ecr.sh \
  --region ap-south-1 \
  --account-id 123456789012 \
  --runner-repo invorto-ai-runner \
  --worker-repo invorto-ai-worker \
  --tag $GIT_HASH

# Also tag as latest
./scripts/push_to_ecr.sh \
  --region ap-south-1 \
  --account-id 123456789012 \
  --runner-repo invorto-ai-runner \
  --worker-repo invorto-ai-worker \
  --tag latest
```

---

## Terraform Deployment

### 1. Configure Terraform Variables

Edit `terraform/terraform.tfvars`:

```hcl
# AWS Configuration
aws_region    = "ap-south-1"
project_name  = "invorto-ai"
ssh_key_name  = "invortoai"  # Must exist in AWS

# Instance Types
runner_instance_type = "t3.small"
worker_instance_type = "t3.small"

# Worker Pool
worker_count = 3  # Number of warm workers (adjust based on traffic)

# ECR Deployment
deploy_runner_from_ecr = true
deploy_workers_from_ecr = true

# ECR Repositories
runner_ecr_repo_name = "invorto-ai-runner"
worker_ecr_repo_name = "invorto-ai-worker"
runner_image_tag     = "latest"
worker_image_tag     = "latest"

# Environment Configuration (SSM Parameter Store)
runner_env_ssm_parameter_name = "/invorto-ai/runner-env"
worker_env_ssm_parameter_name = "/invorto-ai/worker-env"

# Optional: Custom domain
# domain_name = "api.yourdomain.com"
```

### 2. Initialize Terraform

```bash
cd terraform

# Initialize Terraform
terraform init

# Validate configuration
terraform validate

# Preview changes
terraform plan
```

### 3. Deploy Infrastructure

```bash
# Apply infrastructure
terraform apply

# Review the plan and type 'yes' to confirm

# Expected resources created:
# - VPC with public/private subnets
# - Internet Gateway
# - Route tables and associations
# - Security groups (runner, worker, ALB)
# - Application Load Balancer
# - Target group for runner
# - Runner EC2 instance
# - Worker launch template
# - Worker Auto Scaling Group (with N instances)
# - CloudWatch Log Groups
# - IAM roles and policies
# - ECR repositories (if not exist)

# Save outputs
terraform output > ../terraform_outputs.txt
```

### 4. Review Terraform Outputs

```bash
# Get ALB DNS name
terraform output alb_dns_name

# Get runner instance IP
terraform output runner_public_ip

# Get worker instance IPs
terraform output worker_instance_ips

# All outputs
terraform output
```

### Key Terraform Outputs

- **alb_dns_name**: Use for Twilio webhook configuration
- **runner_public_ip**: For SSH access to runner
- **worker_instance_ips**: Worker public IPs (for monitoring)
- **cloudwatch_log_groups**: Log group names for monitoring
- **ecr_repository_urls**: ECR repository URLs

---

## Post-Deployment Configuration

### 1. Verify Services are Running

```bash
# Get ALB DNS name
ALB_DNS=$(cd terraform && terraform output -raw alb_dns_name)

# Check runner health
curl http://$ALB_DNS/health

# Check API documentation
curl http://$ALB_DNS/docs

# Check worker pool status
curl http://$ALB_DNS/workers
```

### 2. SSH into Instances (for debugging)

```bash
# SSH into runner
RUNNER_IP=$(cd terraform && terraform output -raw runner_public_ip)
ssh -i ~/.ssh/invortoai.pem ec2-user@$RUNNER_IP

# On runner instance:
docker ps
docker logs <container-id>

# SSH into worker (get IP from terraform output)
WORKER_IP=$(cd terraform && terraform output -json worker_instance_ips | jq -r '.[0]')
ssh -i ~/.ssh/invortoai.pem ec2-user@$WORKER_IP

# On worker instance:
docker ps
docker logs <container-id>
```

### 3. Update RDS Security Group

Allow EC2 instances to connect to RDS:

```bash
# Get runner security group ID
RUNNER_SG=$(cd terraform && terraform output -raw runner_security_group_id)

# Get worker security group ID
WORKER_SG=$(cd terraform && terraform output -raw worker_security_group_id)

# Get RDS security group ID
RDS_SG=sg-xxxxx  # From RDS console

# Add inbound rules to RDS security group
aws ec2 authorize-security-group-ingress \
  --group-id $RDS_SG \
  --protocol tcp \
  --port 5432 \
  --source-group $RUNNER_SG \
  --region ap-south-1

aws ec2 authorize-security-group-ingress \
  --group-id $RDS_SG \
  --protocol tcp \
  --port 5432 \
  --source-group $WORKER_SG \
  --region ap-south-1
```

### 4. Configure Twilio Webhooks

1. Login to Twilio Console
2. Go to Phone Numbers → Manage → Active Numbers
3. Select your phone number
4. Configure Voice & Fax:
   - **A Call Comes In**: `http://{ALB_DNS}/twilio/incoming` (HTTP POST)
   - **Call Status Changes**: `http://{ALB_DNS}/twilio/status` (HTTP POST)
5. Save configuration

### 5. Configure Jambonz Webhooks (if using)

1. Login to Jambonz console
2. Configure application webhooks:
   - **Call Webhook**: `http://{ALB_DNS}/jambonz/call`
   - **Call Status Webhook**: `http://{ALB_DNS}/jambonz/status`

### 6. Create Assistants and Phone Numbers

```bash
# Get ALB DNS
ALB_DNS=$(cd terraform && terraform output -raw alb_dns_name)

# Create an assistant
curl -X POST http://$ALB_DNS/api/assistants \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "name": "Production Assistant",
    "system_prompt": "You are a helpful AI assistant...",
    "model": "gpt-4o-mini",
    "voice_provider": "elevenlabs",
    "greeting_message": "Hello! How can I help you?"
  }'

# Register phone number
curl -X POST http://$ALB_DNS/api/phone-numbers \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "phone_number": "+1234567890",
    "assistant_id": "<assistant-uuid>",
    "provider": "twilio",
    "twilio_account_sid": "AC...",
    "twilio_auth_token": "...",
    "twilio_sid": "PN..."
  }'
```

---

## Monitoring & Logging

### CloudWatch Logs

View logs in AWS Console or via CLI:

```bash
# View runner logs
aws logs tail /aws/ec2/invorto-ai-runner \
  --follow \
  --region ap-south-1

# View worker logs
aws logs tail /aws/ec2/invorto-ai-worker \
  --follow \
  --region ap-south-1

# Filter logs by error
aws logs filter-log-events \
  --log-group-name /aws/ec2/invorto-ai-runner \
  --filter-pattern "ERROR" \
  --region ap-south-1

# Get log insights
aws logs start-query \
  --log-group-name /aws/ec2/invorto-ai-runner \
  --start-time $(date -u -d '1 hour ago' +%s) \
  --end-time $(date -u +%s) \
  --query-string 'fields @timestamp, @message | filter @message like /ERROR/ | sort @timestamp desc | limit 20' \
  --region ap-south-1
```

### CloudWatch Metrics

Monitor key metrics:

- **EC2 Instance Metrics**: CPU, memory, network
- **ALB Metrics**: Request count, latency, error rates
- **Custom Application Metrics**: Call counts, durations

### Set Up CloudWatch Alarms

```bash
# Create alarm for high CPU on runner
aws cloudwatch put-metric-alarm \
  --alarm-name invorto-ai-runner-high-cpu \
  --alarm-description "Alert when runner CPU exceeds 80%" \
  --metric-name CPUUtilization \
  --namespace AWS/EC2 \
  --statistic Average \
  --period 300 \
  --threshold 80 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 2 \
  --region ap-south-1

# Create alarm for ALB unhealthy targets
aws cloudwatch put-metric-alarm \
  --alarm-name invorto-ai-alb-unhealthy \
  --alarm-description "Alert when ALB has unhealthy targets" \
  --metric-name UnHealthyHostCount \
  --namespace AWS/ApplicationELB \
  --statistic Average \
  --period 60 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --evaluation-periods 1 \
  --region ap-south-1
```

---

## Scaling Strategy

### Vertical Scaling (Instance Size)

Upgrade instance types for better performance:

```hcl
# In terraform/terraform.tfvars
runner_instance_type = "t3.medium"  # Upgrade from t3.small
worker_instance_type = "t3.medium"

# Apply changes
cd terraform
terraform apply
```

### Horizontal Scaling (Worker Count)

Add more workers for higher concurrency:

```hcl
# In terraform/terraform.tfvars
worker_count = 5  # Increase from 3

# Apply changes
cd terraform
terraform apply
```

### Auto Scaling Configuration

The Terraform configuration includes Auto Scaling Groups for workers.

#### Scale Based on Custom Metrics

Track active calls and scale accordingly:

```python
# In application code
import boto3

cloudwatch = boto3.client('cloudwatch', region_name='ap-south-1')

def report_active_calls(count: int):
    cloudwatch.put_metric_data(
        Namespace='InvortoAI',
        MetricData=[
            {
                'MetricName': 'ActiveCalls',
                'Value': count,
                'Unit': 'Count'
            }
        ]
    )
```

---

## CI/CD Pipeline

All code changes go through automated quality checks via Bitbucket Pipelines before reaching production.

### What Runs Automatically

| Trigger | Steps |
|---------|-------|
| Every push | Lint (ruff), security scan (bandit), dependency audit (pip-audit), unit tests (≥40% coverage), type check (mypy) |
| Pull requests | All of the above + integration tests against a Postgres container (≥30% coverage) |
| Merge to `main` | Full suite + Teams notification to the team channel |

### Deploying to Production

The pipeline does **not** automatically deploy to AWS — deployment is still a manual step after the pipeline passes on `main`:

1. Pipeline passes all checks on `main` → Teams notification sent
2. Build and push Docker images to ECR:
   ```bash
   make ecr-push AWS_REGION=ap-south-1 AWS_ACCOUNT_ID=<id> TAG=<git-hash>
   ```
3. Apply Terraform to roll out new image tags:
   ```bash
   cd terraform && terraform apply
   ```

See [bitbucket-pipelines.yml](../bitbucket-pipelines.yml) for the full pipeline definition.

---

## Maintenance & Updates

### Updating Application Code

1. **Build new Docker images**
   ```bash
   # Tag with version
   ./scripts/push_to_ecr.sh \
     --region ap-south-1 \
     --account-id 123456789012 \
     --runner-repo invorto-ai-runner \
     --worker-repo invorto-ai-worker \
     --tag v1.2.0

   # Also update latest
   ./scripts/push_to_ecr.sh \
     --region ap-south-1 \
     --account-id 123456789012 \
     --runner-repo invorto-ai-runner \
     --worker-repo invorto-ai-worker \
     --tag latest
   ```

2. **Update Terraform variables**
   ```hcl
   # terraform/terraform.tfvars
   runner_image_tag = "v1.2.0"
   worker_image_tag = "v1.2.0"
   ```

3. **Apply Terraform**
   ```bash
   cd terraform
   terraform apply
   ```

4. **Rolling restart (zero downtime)**
   ```bash
   # For workers (Auto Scaling handles this)
   aws autoscaling start-instance-refresh \
     --auto-scaling-group-name invorto-ai-worker-asg \
     --region ap-south-1

   # For runner (requires manual intervention)
   # Option 1: SSH and restart container
   ssh -i ~/.ssh/invortoai.pem ec2-user@$RUNNER_IP
   docker pull <ecr-registry>/invorto-ai-runner:v1.2.0
   docker stop <container-id>
   # User data script will restart with new image

   # Option 2: Recreate instance via Terraform taint
   cd terraform
   terraform taint aws_instance.runner
   terraform apply
   ```

### Database Schema Updates

1. **Create new migration file**
   ```bash
   cd migrations
   # Create: 007_your_migration.sql
   ```

2. **Test locally first**
   ```bash
   export DATABASE_URL="postgresql://..."
   python migrate.py --status
   python migrate.py
   ```

3. **Backup production database**
   ```bash
   aws rds create-db-snapshot \
     --db-instance-identifier invorto-ai-db \
     --db-snapshot-identifier invorto-ai-backup-$(date +%Y%m%d-%H%M%S) \
     --region ap-south-1
   ```

4. **Run migration on production**
   ```bash
   # SSH into runner or use bastion
   export DATABASE_URL="postgresql://..."
   python migrate.py
   ```

### Environment Variable Updates

```bash
# Update SSM parameter
aws ssm put-parameter \
  --name "/invorto-ai/runner-env" \
  --type "SecureString" \
  --value "$(cat runner.env)" \
  --region ap-south-1 \
  --overwrite

# Restart services to pick up changes
# See "Rolling restart" section above
```

---

## Troubleshooting

### Issue: Services Not Starting

**Symptoms**: EC2 instances running but containers not starting

**Diagnosis**:
```bash
# SSH into instance
ssh -i ~/.ssh/invortoai.pem ec2-user@$INSTANCE_IP

# Check Docker
sudo docker ps -a
sudo docker logs <container-id>

# Check user data script logs
sudo cat /var/log/cloud-init-output.log

# Check system logs
sudo journalctl -u docker
```

**Common Causes**:
- ECR authentication failed
- Environment variables missing from SSM
- Database connection issues
- Insufficient IAM permissions

### Issue: Workers Not Discovered

**Symptoms**: Runner can't find workers

**Diagnosis**:
```bash
# Check worker pool status
curl http://$ALB_DNS/workers

# Verify EC2 tags
aws ec2 describe-instances \
  --filters "Name=tag:Role,Values=invorto-ai-worker" \
  --region ap-south-1

# Check runner logs
aws logs tail /aws/ec2/invorto-ai-runner --follow
```

**Solutions**:
- Verify `WORKER_POOL_TAG` environment variable
- Check IAM role has `ec2:DescribeInstances` permission
- Ensure workers are in same VPC/region
- Verify worker security group allows traffic from runner

### Issue: Database Connection Failed

**Symptoms**: Applications can't connect to RDS

**Diagnosis**:
```bash
# Test from EC2 instance
ssh -i ~/.ssh/invortoai.pem ec2-user@$RUNNER_IP
psql "postgresql://user:pass@endpoint:5432/dbname"

# Check security groups
aws ec2 describe-security-groups \
  --group-ids <rds-sg-id> \
  --region ap-south-1
```

**Solutions**:
- Add EC2 security groups to RDS inbound rules
- Verify DATABASE_URL is correct
- Check RDS is in running state
- Verify VPC configuration allows communication

### Issue: High Latency

**Symptoms**: Slow response times, timeouts

**Diagnosis**:
```bash
# Check ALB metrics
aws cloudwatch get-metric-statistics \
  --namespace AWS/ApplicationELB \
  --metric-name TargetResponseTime \
  --dimensions Name=LoadBalancer,Value=<alb-arn> \
  --start-time 2024-01-01T00:00:00Z \
  --end-time 2024-01-01T23:59:59Z \
  --period 300 \
  --statistics Average \
  --region ap-south-1

# Check instance CPU/memory
aws cloudwatch get-metric-statistics \
  --namespace AWS/EC2 \
  --metric-name CPUUtilization \
  --dimensions Name=InstanceId,Value=<instance-id> \
  --start-time 2024-01-01T00:00:00Z \
  --end-time 2024-01-01T23:59:59Z \
  --period 300 \
  --statistics Average \
  --region ap-south-1
```

**Solutions**:
- Scale up instance types
- Increase worker count
- Optimize database queries
- Add database connection pooling
- Enable ALB access logs for detailed analysis

### Issue: Workers Running Out of Memory

**Symptoms**: Workers crashing, OOM errors

**Diagnosis**:
```bash
# Check container logs
ssh -i ~/.ssh/invortoai.pem ec2-user@$WORKER_IP
sudo docker stats
sudo docker logs <container-id> | grep -i memory
```

**Solutions**:
- Upgrade to larger instance type (more RAM)
- Reduce `max_tokens` in assistant configuration
- Optimize PyTorch model loading
- Monitor memory leaks

### Issue: SSL/TLS Certificate Errors

**Symptoms**: Twilio can't connect to workers via WSS

**Diagnosis**:
```bash
# Test WebSocket connection
wscat -c wss://<worker-ip>.sslip.io:443/ws

# Check worker logs
aws logs tail /aws/ec2/invorto-ai-worker --follow
```

**Solutions**:
- Verify `.sslip.io` DNS resolves correctly
- Check worker security group allows port 443 inbound
- Ensure proper `WORKER_PUBLIC_WS_*` environment variables
- Consider using custom domain with valid SSL certificate

---

## Additional Resources

### Internal Documentation
- **Local Development Guide**: See LOCAL_DEVELOPMENT.md for local setup
- **Jambonz Setup Guide**: See JAMBONZ_SETUP.md for SIP telephony integration

### External Documentation
- **AWS Documentation**: https://docs.aws.amazon.com/
- **Terraform AWS Provider**: https://registry.terraform.io/providers/hashicorp/aws/
- **Docker Documentation**: https://docs.docker.com/
- **PostgreSQL Documentation**: https://www.postgresql.org/docs/
- **FastAPI Documentation**: https://fastapi.tiangolo.com/
- **Pipecat Framework**: https://github.com/pipecat-ai/pipecat
- **Jambonz Documentation**: https://docs.jambonz.org/

---

## Appendix

### A. Complete AWS CLI Commands Reference

See sections above for specific commands.

### B. Terraform Resource Graph

```bash
cd terraform
terraform graph | dot -Tpng > infrastructure.png
```

### C. Security Group Rules

| Service | Port | Protocol | Source | Purpose |
|---------|------|----------|--------|---------|
| ALB | 80 | TCP | 0.0.0.0/0 | HTTP traffic |
| ALB | 443 | TCP | 0.0.0.0/0 | HTTPS traffic (optional) |
| Runner | 7860 | TCP | ALB SG | ALB to runner |
| Runner | 22 | TCP | Admin IP | SSH access |
| Worker | 443 | TCP | 0.0.0.0/0 | WSS from Twilio |
| Worker | 8765 | TCP | Runner SG | Internal health checks |
| Worker | 22 | TCP | Admin IP | SSH access |
| RDS | 5432 | TCP | Runner SG, Worker SG | Database access |

---

**Last Updated**: 2026-01-15
