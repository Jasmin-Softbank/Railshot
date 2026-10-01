variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "name" {
  type    = string
  default = "railshot"
}

variable "instance_type" {
  type    = string
  default = "t3.large" # Nitro x86_64 only: device naming is an explicit profile contract.
  validation {
    condition     = contains(["t3.large", "t3.xlarge", "t3.2xlarge"], var.instance_type)
    error_message = "Reviewed Nitro profile supports t3.large, t3.xlarge, t3.2xlarge only."
  }
}

variable "root_volume_gb" {
  type    = number
  default = 40
}

variable "github_repo" {
  description = "owner/repo allowed to assume the read-only CI role via OIDC"
  type        = string
  default     = "Jasmin-Softbank/Jasmin"
}

variable "budget_usd" {
  type    = number
  default = 30
}

variable "budget_email" {
  description = "Budget alert recipient; empty disables the budget"
  type        = string
  default     = ""
}

variable "https_enabled" {
  description = "Open 443 once the app domain and its certificate exist; until then only 80 is reachable"
  type        = bool
  default     = false
}

variable "ami_id" {
  type        = string
  description = "Reviewed exact Canonical Ubuntu 24.04 amd64 AMI in this region; no moving SSM image alias."
  validation {
    condition     = can(regex("^ami-[0-9a-f]{17}$", var.ami_id))
    error_message = "ami_id must be an exact regional AMI ID."
  }
}
variable "target_id" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,39}$", var.target_id))
    error_message = "target_id must be a registered lowercase alias."
  }
}
variable "account_id" {
  type        = string
  description = "Registered AWS account; provider rejects credentials for another account."
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be the explicitly registered 12-digit account."
  }
}
variable "data_disk_gib" {
  type    = number
  default = 100
  validation {
    condition     = var.data_disk_gib >= 20 && var.data_disk_gib <= 1000 && floor(var.data_disk_gib) == var.data_disk_gib
    error_message = "data_disk_gib must be 20-1000 whole GiB; shrinking existing data is unsupported."
  }
}
variable "initialize_empty_data_disk" {
  type        = bool
  default     = false
  description = "Consent to initialize this module-created blank disk only. Existing ext4 is never reformatted."
}
variable "owner_ref" {
  type        = string
  description = "Authoritative administrator state binding; common executor injects its exact state path."
  validation {
    condition     = startswith(var.owner_ref, "terraform:") && length(var.owner_ref) > 10 && length(var.owner_ref) < 2048
    error_message = "owner_ref must identify the registered Terraform state owner."
  }
}

variable "node_name" {
  type        = string
  default     = ""
  description = "Desired identity passed to the team runtime owner; no runtime is installed here. Stable Kubernetes node identity; empty uses name for NEW nodes. Existing clusters must preserve their original registered name, not the current cloud hostname."
  validation {
    condition     = var.node_name == "" || (length(var.node_name) <= 253 && can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$", var.node_name)))
    error_message = "node_name must be empty or a lowercase DNS subdomain, at most 253 characters. Renaming an existing node is not a normal bootstrap update."
  }
}
