# Premium is required for this private endpoint design. No SAS auth in the lab.
resource "azurerm_servicebus_namespace" "lab" {
  name                          = var.servicebus_namespace_name
  resource_group_name           = azurerm_resource_group.lab.name
  location                      = azurerm_resource_group.lab.location
  sku                           = "Premium"
  capacity                      = 1
  premium_messaging_partitions  = 1
  minimum_tls_version           = "1.2"
  local_auth_enabled            = false
  public_network_access_enabled = false
  tags                          = local.tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_servicebus_queue" "reservation_events" {
  name                                 = "reservation-events"
  namespace_id                         = azurerm_servicebus_namespace.lab.id
  lock_duration                        = "PT1M"
  default_message_ttl                  = "P14D"
  max_delivery_count                   = 5
  max_size_in_megabytes                = 1024
  dead_lettering_on_message_expiration = true
  batched_operations_enabled           = true
  partitioning_enabled                 = false
  requires_session                     = false
  requires_duplicate_detection         = false

  # Consumers still deduplicate durable message IDs; broker dedup is not enabled.
  lifecycle {
    prevent_destroy = true
  }
}

resource "azurerm_private_dns_zone" "servicebus" {
  name                = "privatelink.servicebus.windows.net"
  resource_group_name = azurerm_resource_group.lab.name
  tags                = local.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "servicebus" {
  name                 = "${local.prefix}-servicebus-dns"
  private_dns_zone_id  = azurerm_private_dns_zone.servicebus.id
  virtual_network_id   = azurerm_virtual_network.lab.id
  registration_enabled = false
  tags                 = local.tags
}

resource "azurerm_private_endpoint" "servicebus" {
  name                = "${local.prefix}-servicebus-pe"
  location            = azurerm_virtual_network.lab.location
  resource_group_name = azurerm_resource_group.lab.name
  subnet_id           = azurerm_subnet.endpoints.id
  tags                = local.tags

  private_service_connection {
    name                           = "${local.prefix}-servicebus"
    private_connection_resource_id = azurerm_servicebus_namespace.lab.id
    subresource_names              = ["namespace"]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "servicebus"
    private_dns_zone_ids = [azurerm_private_dns_zone.servicebus.id]
  }
}

resource "azurerm_role_assignment" "publisher_servicebus_sender" {
  scope                = azurerm_servicebus_queue.reservation_events.id
  role_definition_name = "Azure Service Bus Data Sender"
  principal_id         = azurerm_user_assigned_identity.workload["publisher"].principal_id
  principal_type       = "ServicePrincipal"
}

output "servicebus_namespace_name" {
  value = azurerm_servicebus_namespace.lab.name
}

output "servicebus_fully_qualified_namespace" {
  value = "${azurerm_servicebus_namespace.lab.name}.servicebus.windows.net"
}

output "servicebus_queue_name" {
  value = azurerm_servicebus_queue.reservation_events.name
}

output "servicebus_private_endpoint_id" {
  value = azurerm_private_endpoint.servicebus.id
}
