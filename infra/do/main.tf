# IdleGrid coordinator on one DigitalOcean droplet in Bangalore.
# Caddy terminates HTTPS (Let's Encrypt) on a free <ip>.sslip.io hostname and
# proxies to the coordinator on 127.0.0.1:8000. No GPU: nodes do the inference.
#
# The API token is read from the DIGITALOCEAN_TOKEN environment variable
# (infra/deploy.sh loads it from ~/.config/idlegrid/do_token); it never goes in a file here.

terraform {
  required_version = ">= 1.6"
  required_providers {
    digitalocean = { source = "digitalocean/digitalocean", version = ">= 2.40" }
  }
}

variable "region" {
  type    = string
  default = "blr1" # Bangalore
}
variable "size" {
  type    = string
  default = "s-1vcpu-1gb" # 1 vCPU, 1 GB RAM, 25 GB SSD
}
variable "ssh_cidr" {
  type        = string
  description = "Your public IP in CIDR form; the only address allowed to SSH in."
}
variable "ssh_public_key_path" {
  type    = string
  default = "../idlegrid_deploy_key.pub"
}

provider "digitalocean" {}

resource "digitalocean_ssh_key" "deploy" {
  name       = "idlegrid-deploy"
  public_key = file(var.ssh_public_key_path)
}

resource "digitalocean_droplet" "coordinator" {
  name       = "idlegrid-coordinator"
  image      = "ubuntu-24-04-x64"
  region     = var.region
  size       = var.size
  ssh_keys   = [digitalocean_ssh_key.deploy.fingerprint]
  user_data  = file("${path.module}/../cloud-init.sh")
  ipv6       = true
  monitoring = true
  tags       = ["idlegrid"]
}

# A reserved IP survives droplet rebuilds, so the URL never changes. Free while attached.
resource "digitalocean_reserved_ip" "coordinator" {
  droplet_id = digitalocean_droplet.coordinator.id
  region     = var.region
}

resource "digitalocean_firewall" "coordinator" {
  name        = "idlegrid-coordinator"
  droplet_ids = [digitalocean_droplet.coordinator.id]

  inbound_rule {
    protocol         = "tcp"
    port_range       = "80"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }
  inbound_rule {
    protocol         = "tcp"
    port_range       = "443"
    source_addresses = ["0.0.0.0/0", "::/0"]
  }
  inbound_rule {
    protocol         = "tcp"
    port_range       = "22"
    source_addresses = [var.ssh_cidr]
  }
  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}

locals {
  hostname = "${replace(digitalocean_reserved_ip.coordinator.ip_address, ".", "-")}.sslip.io"
}

output "public_ip" {
  value = digitalocean_reserved_ip.coordinator.ip_address
}
output "hostname" {
  value = local.hostname
}
output "url" {
  value = "https://${local.hostname}"
}
output "ssh_user" {
  value = "root"
}
