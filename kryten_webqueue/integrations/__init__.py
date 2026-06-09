"""Vendored third-party tooling adapted for in-process use by webqueue jobs.

Subpackages here are copied (and lightly adapted) from external repositories
so their battle-tested logic can run inside the webqueue service without
shelling out. Each vendored module carries a header noting its upstream source
and the date it was vendored; adapters are kept thin so re-vendoring stays
mechanical.
"""
