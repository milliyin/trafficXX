terraform {
  required_version = ">= 1.16.5, < 1.17.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "= 5.10.0"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id                 = var.subscription_id
  storage_use_azuread             = true
  resource_provider_registrations = "none"
}

variable "subscription_id" {
  type        = string
  description = "Azure subscription to provision into."
}

variable "location" {
  type        = string
  description = "Azure region; supplied at deployment time."
}

variable "state_account_name" {
  type        = string
  description = "Globally unique state account name, 3-24 lowercase letters or digits."
  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.state_account_name))
    error_message = "Use 3-24 lowercase letters or digits."
  }
}

resource "azurerm_resource_group" "state" {
  name     = "trafficxx-tfstate-rg"
  location = var.location
  tags     = { project = "trafficxx", purpose = "terraform-state" }
}

resource "azurerm_storage_account" "state" {
  name                            = var.state_account_name
  resource_group_name             = azurerm_resource_group.state.name
  location                        = azurerm_resource_group.state.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  tags                            = azurerm_resource_group.state.tags

  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# The ARM resource avoids requiring blob data-plane RBAC just to bootstrap.
resource "azurerm_storage_container" "state" {
  name                  = "tfstate"
  storage_account_id    = azurerm_storage_account.state.id
  container_access_type = "private"
  lifecycle {
    prevent_destroy = true
  }
}

output "backend" {
  value = {
    resource_group_name  = azurerm_resource_group.state.name
    storage_account_name = azurerm_storage_account.state.name
    container_name       = azurerm_storage_container.state.name
    key                  = "lab/foundation.tfstate"
    use_azuread_auth     = true
  }
}
