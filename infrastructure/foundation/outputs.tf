output "resource_group_name" {
  value = azurerm_resource_group.lab.name
}

output "aks_subnet_id" {
  value = azurerm_subnet.aks.id
}

output "postgres_subnet_id" {
  value = azurerm_subnet.postgres_regional.id
}

output "private_endpoint_subnet_id" {
  value = azurerm_subnet.endpoints.id
}

output "registry_login_server" {
  value = azurerm_container_registry.lab.login_server
}

output "log_analytics_workspace_id" {
  value = azurerm_log_analytics_workspace.lab.id
}

output "aks_cluster_name" {
  value = azurerm_kubernetes_cluster.lab.name
}

output "aks_oidc_issuer_url" {
  value = azurerm_kubernetes_cluster.lab.oidc_issuer_url
}

output "aks_kubelet_client_id" {
  value = azurerm_user_assigned_identity.aks_kubelet.client_id
}

output "postgres_server_name" {
  value = azurerm_postgresql_flexible_server.lab.name
}

output "postgres_fqdn" {
  value = azurerm_postgresql_flexible_server.lab.fqdn
}

output "postgres_database_name" {
  value = azurerm_postgresql_flexible_server_database.trafficxx.name
}

output "postgres_location" {
  value = azurerm_postgresql_flexible_server.lab.location
}
