# Opt-in address reservation only. No Service/listener is published by Terraform.
resource "azurerm_public_ip" "grafana" {
  count = var.enable_grafana_public_endpoint ? 1 : 0

  name                    = "${local.prefix}-grafana-ip"
  resource_group_name     = azurerm_resource_group.lab.name
  location                = azurerm_resource_group.lab.location
  allocation_method       = "Static"
  sku                     = "Standard"
  sku_tier                = "Regional"
  ip_version              = "IPv4"
  domain_name_label       = var.grafana_dns_label
  domain_name_label_scope = "SubscriptionReuse"
  tags                    = local.tags
}

# The AKS control-plane identity attaches this address to its load balancer.
# Scope is this public IP, not the subscription or all networks in the lab.
resource "azurerm_role_assignment" "aks_grafana_public_ip" {
  count = var.enable_grafana_public_endpoint ? 1 : 0

  scope                            = azurerm_public_ip.grafana[0].id
  role_definition_name             = "Network Contributor"
  principal_id                     = azurerm_user_assigned_identity.aks_control_plane.principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

output "grafana_public_endpoint" {
  description = "Reserved address metadata; an HTTPS listener is a separate deployment step."
  value = var.enable_grafana_public_endpoint ? {
    public_ip_name      = azurerm_public_ip.grafana[0].name
    resource_group_name = azurerm_public_ip.grafana[0].resource_group_name
    ip_address          = azurerm_public_ip.grafana[0].ip_address
    hostname            = azurerm_public_ip.grafana[0].fqdn
  } : null
}
