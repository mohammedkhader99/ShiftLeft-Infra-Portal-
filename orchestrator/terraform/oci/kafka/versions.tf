terraform {
  # 1.9 is the floor because vcn_ocid's validation references create_nsg, and
  # cross-variable validation references landed in 1.9.
  required_version = ">= 1.9.0"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = ">= 5.0.0"
    }
  }
}
