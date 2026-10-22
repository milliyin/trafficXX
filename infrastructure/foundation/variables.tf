variable "subscription_id" {
  type        = string
  description = "Azure subscription to provision into."
}

variable "location" {
  type        = string
  description = "Azure region; eastus2 in examples is only a placeholder."
}

variable "registry_name" {
  type        = string
  description = "Globally unique ACR name, 5-50 lowercase letters or digits."
  validation {
    condition     = can(regex("^[a-z0-9]{5,50}$", var.registry_name))
    error_message = "Use 5-50 lowercase letters or digits."
  }
}

variable "aks_kubernetes_version" {
  type        = string
  description = "AKS version available in the selected region; verified via user-run CLI."
  default     = "1.36.4"
}

variable "aks_node_vm_size" {
  type        = string
  description = "VM size for both node pools; verify subscription SKU availability before apply."
  default     = "Standard_D4s_v5"
}

variable "aks_admin_object_id" {
  type        = string
  description = "Entra object ID of the intended human lab administrator (not a subscription or application ID)."
  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.aks_admin_object_id))
    error_message = "Supply the administrator's Entra object ID as a UUID."
  }
}

variable "postgres_server_name" {
  type        = string
  description = "Globally unique Azure PostgreSQL server name; availability checked by Azure."
  default     = "trafficxx-pg-e8708a85"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,61}[a-z0-9]$", var.postgres_server_name))
    error_message = "Use 3-63 lowercase letters, digits, or hyphens; begin with a letter and end with a letter or digit."
  }
}

variable "postgres_location" {
  type        = string
  description = "Database/VNet region, separate from the existing AKS region due to provisioning restrictions."
  default     = "centralus"
}

variable "postgres_sku_name" {
  type        = string
  description = "PostgreSQL compute SKU; verify availability in the selected region."
  default     = "GP_Standard_D2ds_v5"
}

variable "postgres_admin_principal_name" {
  type        = string
  description = "Entra user principal name corresponding to aks_admin_object_id, used as the human PostgreSQL administrator."
  validation {
    condition     = length(trimspace(var.postgres_admin_principal_name)) > 0
    error_message = "Supply the administrator's Entra user principal name."
  }
}

variable "servicebus_namespace_name" {
  type        = string
  description = "Globally unique Service Bus namespace name; availability and Premium capacity must be checked in Azure."
  default     = "trafficxx-sb-e8708a85"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{4,48}[a-z0-9]$", var.servicebus_namespace_name))
    error_message = "Use 6-50 lowercase letters, digits or hyphens; start with a letter and end with a letter or digit."
  }
}
