# production/admin.py
"""
Deliberately empty.

Every model in this app is farm-operational or business data (houses,
batches, daily records, weight samples, harvests, sales, payments, feed,
inventory, routine tasks) — the technical administrator who uses this
admin creates owner accounts and maintains accounts; they take no part in
farm operations, so none of it is registered here. The models and their
business logic are untouched; only the admin registrations (and the
TabularInlines that existed solely to show them) are gone. See the admin
apps in accounts/ and farms/ for what the admin still covers.
"""
