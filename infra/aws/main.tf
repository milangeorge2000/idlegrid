# IdleGrid coordinator on one small ARM EC2 instance in Mumbai.
# Caddy terminates HTTPS (Let's Encrypt) on a free <ip>.sslip.io hostname and
# proxies to the coordinator on 127.0.0.1:8000. No GPU: nodes do the inference.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 5.60" }
  }
}

variable "region" {
  type    = string
  default = "ap-south-1"
}
variable "aws_profile" {
  type    = string
  default = "idlegrid"
}
variable "instance_type" {
  type    = string
  default = "t4g.small" # 2 vCPU Graviton, 2 GB RAM
}
variable "ssh_cidr" {
  type        = string
  description = "Your public IP in CIDR form; the only address allowed to SSH in."
}
variable "ssh_public_key_path" {
  type    = string
  default = "../idlegrid_deploy_key.pub"
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
  default_tags {
    tags = { Project = "idlegrid", ManagedBy = "terraform" }
  }
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"]
  }
}

data "aws_vpc" "default" {
  default = true
}

resource "aws_key_pair" "deploy" {
  key_name   = "idlegrid-deploy"
  public_key = file(var.ssh_public_key_path)
}

resource "aws_security_group" "coordinator" {
  name        = "idlegrid-coordinator"
  description = "HTTPS for buyers and nodes; SSH only from the owner"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description      = "HTTP (Let's Encrypt challenge + redirect)"
    from_port        = 80
    to_port          = 80
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
  ingress {
    description      = "HTTPS API, dashboard and node WebSockets"
    from_port        = 443
    to_port          = 443
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
  ingress {
    description = "SSH from the owner only"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.ssh_cidr]
  }
  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
}

resource "aws_instance" "coordinator" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  key_name               = aws_key_pair.deploy.key_name
  vpc_security_group_ids = [aws_security_group.coordinator.id]
  user_data              = file("${path.module}/../cloud-init.sh")

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }
  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }
  tags = { Name = "idlegrid-coordinator" }
}

resource "aws_eip" "coordinator" {
  instance = aws_instance.coordinator.id
  domain   = "vpc"
  tags     = { Name = "idlegrid-coordinator" }
}

locals {
  hostname = "${replace(aws_eip.coordinator.public_ip, ".", "-")}.sslip.io"
}

output "public_ip" {
  value = aws_eip.coordinator.public_ip
}
output "hostname" {
  value = local.hostname
}
output "url" {
  value = "https://${local.hostname}"
}
output "ssh" {
  value = "ssh -i infra/idlegrid_deploy_key ubuntu@${aws_eip.coordinator.public_ip}"
}

output "ssh_user" {
  value = "ubuntu"
}
