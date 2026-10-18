resource "azurerm_private_dns_zone" "postgres" {
  name                = "trafficxx.postgres.database.azure.com"
  resource_group_name = azurerm_resource_group.lab.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
  name                 = "${local.prefix}-postgres-dns"
  private_dns_zone_id  = azurerm_private_dns_zone.postgres.id
  virtual_network_id   = azurerm_virtual_network.lab.id
  registration_enabled = false
  tags                 = local.tags
}

resource "azurerm_postgresql_flexible_server" "lab" {
  name                          = var.postgres_server_name
  resource_group_name           = azurerm_resource_group.lab.name
  location                      = var.postgres_location
  version                       = "17"
  sku_name                      = var.postgres_sku_name
  storage_mb                    = 32768
  auto_grow_enabled             = true
  backup_retention_days         = 14
  geo_redundant_backup_enabled  = false
  delegated_subnet_id           = azurerm_subnet.postgres_regional.id
  private_dns_zone_id           = azurerm_private_dns_zone.postgres.id
  public_network_access_enabled = false
  tags                          = local.tags

  authentication {
    active_directory_auth_enabled = true
    password_auth_enabled         = false
    tenant_id                     = data.azurerm_client_config.current.tenant_id
  }

  lifecycle {
    prevent_destroy = true
    # Azure chooses placement when no zone is requested. Preserve that decision
    # rather than repeatedly planning to clear it (or undoing a future failover).
    ignore_changes = [zone]
  }

  depends_on = [
    azurerm_private_dns_zone_virtual_network_link.postgres,
    azurerm_private_dns_zone_virtual_network_link.postgres_regional,
  ]
}

# The same explicitly selected human can administer AKS and PostgreSQL for this
# lab. Application identities will receive their own database roles later.
resource "azurerm_postgresql_flexible_server_active_directory_administrator" "lab" {
  server_name         = azurerm_postgresql_flexible_server.lab.name
  resource_group_name = azurerm_resource_group.lab.name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  object_id           = var.aks_admin_object_id
  principal_name      = var.postgres_admin_principal_name
  principal_type      = "User"
}

resource "azurerm_postgresql_flexible_server_database" "trafficxx" {
  name      = "trafficxx"
  server_id = azurerm_postgresql_flexible_server.lab.id
  charset   = "UTF8"
  collation = "en_US.utf8"

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [azurerm_postgresql_flexible_server_active_directory_administrator.lab]
}

resource "azurerm_postgresql_flexible_server_configuration" "require_tls" {
  name      = "require_secure_transport"
  server_id = azurerm_postgresql_flexible_server.lab.id
  value     = "on"
}
