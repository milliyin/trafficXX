# PostgreSQL provisioning is restricted in East US for this subscription.
# A database and its delegated subnet must share a region, so keep AKS's existing
# VNet intact and add a separate database VNet in the permitted region.
resource "azurerm_virtual_network" "postgres" {
  name                = "${local.prefix}-postgres-vnet"
  resource_group_name = azurerm_resource_group.lab.name
  location            = var.postgres_location
  address_space       = ["10.50.0.0/16"]
  tags                = local.tags
}

resource "azurerm_subnet" "postgres_regional" {
  name                 = "postgres"
  resource_group_name  = azurerm_resource_group.lab.name
  virtual_network_name = azurerm_virtual_network.postgres.name
  address_prefixes     = ["10.50.0.0/24"]

  service_endpoint {
    service = "Microsoft.Storage"
  }

  delegation {
    name = "postgres-flexible-server"
    service_delegation {
      name    = "Microsoft.DBforPostgreSQL/flexibleServers"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_virtual_network_peering" "aks_to_postgres" {
  name                         = "aks-to-postgres"
  resource_group_name          = azurerm_resource_group.lab.name
  virtual_network_name         = azurerm_virtual_network.lab.name
  remote_virtual_network_id    = azurerm_virtual_network.postgres.id
  allow_virtual_network_access = true
  allow_forwarded_traffic      = false
  allow_gateway_transit        = false
  use_remote_gateways          = false
}

resource "azurerm_virtual_network_peering" "postgres_to_aks" {
  name                         = "postgres-to-aks"
  resource_group_name          = azurerm_resource_group.lab.name
  virtual_network_name         = azurerm_virtual_network.postgres.name
  remote_virtual_network_id    = azurerm_virtual_network.lab.id
  allow_virtual_network_access = true
  allow_forwarded_traffic      = false
  allow_gateway_transit        = false
  use_remote_gateways          = false

  # Serialize the two sides to avoid overlapping Azure peering operations.
  depends_on = [azurerm_virtual_network_peering.aks_to_postgres]
}

resource "azurerm_private_dns_zone_virtual_network_link" "postgres_regional" {
  name                 = "${local.prefix}-postgres-regional-dns"
  private_dns_zone_id  = azurerm_private_dns_zone.postgres.id
  virtual_network_id   = azurerm_virtual_network.postgres.id
  registration_enabled = false
  tags                 = local.tags
}
