data "azurerm_client_config" "current" {}

# Identities are created before AKS so their permissions can be assigned first.
resource "azurerm_user_assigned_identity" "aks_control_plane" {
  name                = "${local.prefix}-aks-control-plane"
  resource_group_name = azurerm_resource_group.lab.name
  location            = azurerm_resource_group.lab.location
  tags                = local.tags
}

resource "azurerm_user_assigned_identity" "aks_kubelet" {
  name                = "${local.prefix}-aks-kubelet"
  resource_group_name = azurerm_resource_group.lab.name
  location            = azurerm_resource_group.lab.location
  tags                = local.tags
}

resource "azurerm_role_assignment" "aks_subnet" {
  scope                            = azurerm_subnet.aks.id
  role_definition_name             = "Network Contributor"
  principal_id                     = azurerm_user_assigned_identity.aks_control_plane.principal_id
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "aks_kubelet_operator" {
  scope                            = azurerm_user_assigned_identity.aks_kubelet.id
  role_definition_name             = "Managed Identity Operator"
  principal_id                     = azurerm_user_assigned_identity.aks_control_plane.principal_id
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "aks_image_pull" {
  scope                            = azurerm_container_registry.lab.id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_user_assigned_identity.aks_kubelet.principal_id
  skip_service_principal_aad_check = true
}

resource "azurerm_kubernetes_cluster" "lab" {
  name                              = "${local.prefix}-aks"
  resource_group_name               = azurerm_resource_group.lab.name
  location                          = azurerm_resource_group.lab.location
  dns_prefix                        = "${local.prefix}-aks"
  node_resource_group               = "${local.prefix}-aks-nodes-rg"
  kubernetes_version                = var.aks_kubernetes_version
  sku_tier                          = "Standard"
  support_plan                      = "KubernetesOfficial"
  local_account_disabled            = true
  role_based_access_control_enabled = true
  oidc_issuer_enabled               = true
  workload_identity_enabled         = true
  node_os_upgrade_channel           = "NodeImage"
  private_cluster_enabled           = false
  tags                              = local.tags

  workload_autoscaler_profile {
    keda_enabled = var.enable_browse_keda
  }

  # Terraform owns pool definitions; AKS's cluster autoscaler adjusts app capacity.
  node_provisioning_profile {
    mode = "Manual"
  }

  default_node_pool {
    name                         = "system"
    vm_size                      = var.aks_node_vm_size
    node_count                   = 2
    max_pods                     = 30
    vnet_subnet_id               = azurerm_subnet.aks.id
    os_sku                       = "Ubuntu"
    os_disk_type                 = "Managed"
    os_disk_size_gb              = 64
    only_critical_addons_enabled = true
    temporary_name_for_rotation  = "systemtemp"
    node_public_ip_enabled       = false
    tags                         = local.tags

    upgrade_settings {
      max_surge = "1"
    }
  }

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.aks_control_plane.id]
  }

  kubelet_identity {
    client_id                 = azurerm_user_assigned_identity.aks_kubelet.client_id
    object_id                 = azurerm_user_assigned_identity.aks_kubelet.principal_id
    user_assigned_identity_id = azurerm_user_assigned_identity.aks_kubelet.id
  }

  azure_active_directory_role_based_access_control {
    tenant_id          = data.azurerm_client_config.current.tenant_id
    azure_rbac_enabled = true
  }

  # The lab API is public for Cloud Shell access; Entra authentication is required.
  # Private API connectivity / source-IP restrictions are a later hardening step.
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_data_plane  = "cilium"
    network_policy      = "cilium"
    pod_cidr            = "10.244.0.0/16"
    service_cidr        = "10.41.0.0/16"
    dns_service_ip      = "10.41.0.10"
    load_balancer_sku   = "standard"
    outbound_type       = "loadBalancer"

    load_balancer_profile {
      managed_outbound_ip_count = 1
    }
  }

  depends_on = [
    azurerm_role_assignment.aks_subnet,
    azurerm_role_assignment.aks_kubelet_operator,
    azurerm_role_assignment.aks_image_pull,
  ]
}

resource "azurerm_kubernetes_cluster_node_pool" "apps" {
  name                        = "apps"
  kubernetes_cluster_id       = azurerm_kubernetes_cluster.lab.id
  mode                        = "User"
  vm_size                     = var.aks_node_vm_size
  node_count                  = 2
  auto_scaling_enabled        = true
  min_count                   = 2
  max_count                   = 4
  max_pods                    = 30
  vnet_subnet_id              = azurerm_subnet.aks.id
  os_sku                      = "Ubuntu"
  os_disk_type                = "Managed"
  os_disk_size_gb             = 64
  node_public_ip_enabled      = false
  temporary_name_for_rotation = "appstemp"
  node_labels                 = { workload = "trafficxx" }
  tags                        = local.tags

  upgrade_settings {
    max_surge = "1"
  }

  lifecycle {
    ignore_changes = [node_count]
  }
}

# Pin the intended human administrator as an input rather than following the
# identity of whoever happens to execute Terraform (for example, a future CI job).
resource "azurerm_role_assignment" "aks_lab_admin" {
  scope                = azurerm_kubernetes_cluster.lab.id
  role_definition_name = "Azure Kubernetes Service RBAC Cluster Admin"
  principal_id         = var.aks_admin_object_id
}
