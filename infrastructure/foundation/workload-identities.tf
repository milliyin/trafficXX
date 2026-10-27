locals {
  workload_namespace = "trafficxx"
  workload_accounts = setunion(toset([
    "browse", "booking", "expiry", "publisher", "consumer", "operations",
    "migrator", "reconciler",
  ]), var.enable_clickclash ? toset(["clickclash"]) : toset([]))
}

resource "azurerm_user_assigned_identity" "workload" {
  for_each            = local.workload_accounts
  name                = "${local.prefix}-${each.key}"
  resource_group_name = azurerm_resource_group.lab.name
  location            = azurerm_resource_group.lab.location
  tags                = merge(local.tags, { workload = each.key })
}

# Trust is limited to this cluster, namespace, and named service account.
# Federation authenticates the workload; it does not grant SQL or Azure access.
resource "azurerm_federated_identity_credential" "workload" {
  for_each                  = local.workload_accounts
  name                      = "aks-${each.key}"
  user_assigned_identity_id = azurerm_user_assigned_identity.workload[each.key].id
  audience                  = ["api://AzureADTokenExchange"]
  issuer                    = azurerm_kubernetes_cluster.lab.oidc_issuer_url
  subject                   = "system:serviceaccount:${local.workload_namespace}:${each.key}"
}

# These identifiers are not access tokens or secrets. SQL bootstrap will use
# principal_id; Kubernetes service accounts use client_id.
output "workload_identities" {
  value = {
    for name, identity in azurerm_user_assigned_identity.workload : name => {
      client_id       = identity.client_id
      principal_id    = identity.principal_id
      resource_id     = identity.id
      tenant_id       = identity.tenant_id
      namespace       = local.workload_namespace
      service_account = name
      subject         = azurerm_federated_identity_credential.workload[name].subject
    }
  }
}

# Export after apply and review before kubectl apply. Terraform's Azure provider
# does not create these Kubernetes objects or require Kubernetes credentials.
output "workload_service_accounts_manifest" {
  value = jsonencode({
    apiVersion = "v1"
    kind       = "List"
    items = concat(
      [{
        apiVersion = "v1"
        kind       = "Namespace"
        metadata = {
          name   = local.workload_namespace
          labels = { "app.kubernetes.io/part-of" = "trafficxx" }
        }
      }],
      [for name in sort(tolist(local.workload_accounts)) : {
        apiVersion = "v1"
        kind       = "ServiceAccount"
        metadata = {
          name      = name
          namespace = local.workload_namespace
          labels    = { "app.kubernetes.io/part-of" = "trafficxx" }
          annotations = {
            "azure.workload.identity/client-id" = azurerm_user_assigned_identity.workload[name].client_id
            "azure.workload.identity/tenant-id" = azurerm_user_assigned_identity.workload[name].tenant_id
          }
        }
        automountServiceAccountToken = false
      }],
    )
  })
}
