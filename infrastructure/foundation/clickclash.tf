# Opt-in resources only. The existing Browse/Grafana policy is untouched.
variable "enable_clickclash" {
  type        = bool
  default     = false
  description = "Provision ClickClash workload identity and reserved HTTPS gateway address."
}

resource "azurerm_public_ip" "clickclash" {
  count                   = var.enable_clickclash ? 1 : 0
  name                    = "${local.prefix}-clickclash-ip"
  resource_group_name     = azurerm_resource_group.lab.name
  location                = azurerm_resource_group.lab.location
  allocation_method       = "Static"
  sku                     = "Standard"
  domain_name_label       = "clickclash-e8708a85"
  domain_name_label_scope = "SubscriptionReuse"
  tags                    = local.tags
}

resource "azurerm_role_assignment" "aks_clickclash_public_ip" {
  count                            = var.enable_clickclash ? 1 : 0
  scope                            = azurerm_public_ip.clickclash[0].id
  role_definition_name             = "Network Contributor"
  principal_id                     = azurerm_user_assigned_identity.aks_control_plane.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

output "clickclash_endpoint" {
  value = var.enable_clickclash ? {
    hostname = azurerm_public_ip.clickclash[0].fqdn
    address  = azurerm_public_ip.clickclash[0].ip_address
  } : null
}
