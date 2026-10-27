variable "enable_clickclash_blob" {
  type    = bool
  default = false
  validation {
    condition     = !var.enable_clickclash_blob || var.enable_clickclash
    error_message = "ClickClash storage requires its workload identity."
  }
}

resource "azurerm_storage_account" "clickclash" {
  count                           = var.enable_clickclash_blob ? 1 : 0
  name                            = "trafficxxcce8708a85"
  resource_group_name             = azurerm_resource_group.lab.name
  location                        = azurerm_resource_group.lab.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  tags                            = local.tags
  blob_properties {
    versioning_enabled = true
    delete_retention_policy { days = 14 }
    container_delete_retention_policy { days = 14 }
  }
  lifecycle { prevent_destroy = true }
}

resource "azurerm_storage_container" "clickclash" {
  count                 = var.enable_clickclash_blob ? 1 : 0
  name                  = "banners"
  storage_account_id    = azurerm_storage_account.clickclash[0].id
  container_access_type = "private"
  lifecycle { prevent_destroy = true }
}

resource "azurerm_role_assignment" "clickclash_blobs" {
  count              = var.enable_clickclash_blob ? 1 : 0
  scope              = "${azurerm_storage_account.clickclash[0].id}/blobServices/default/containers/${azurerm_storage_container.clickclash[0].name}"
  role_definition_id = azurerm_role_definition.clickclash_blobs[0].role_definition_resource_id
  principal_id       = azurerm_user_assigned_identity.workload["clickclash"].principal_id
  principal_type     = "ServicePrincipal"
}

resource "azurerm_role_definition" "clickclash_blobs" {
  count             = var.enable_clickclash_blob ? 1 : 0
  name              = "trafficxx-clickclash-banner-objects"
  scope             = azurerm_resource_group.lab.id
  assignable_scopes = [azurerm_resource_group.lab.id]
  permissions {
    actions = ["Microsoft.Storage/storageAccounts/blobServices/containers/read"]
    data_actions = [
      "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read",
      "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write",
      "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/delete"
    ]
  }
}
