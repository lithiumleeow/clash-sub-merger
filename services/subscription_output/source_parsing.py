"""Post-merge source parsing support for the subscription endpoint.

Phase (g) of the ``/sub`` pipeline: after ``ConfigMerger`` parsed and
transformed every source off the event loop, apply per-node user allocations
and regenerate/override the proxy groups accordingly.
"""

from typing import Callable, Optional

from services.config_merger import ProxyGroupGenerator
from services.country_grouper import CountryGrouper

from .pool_assembly import is_allocated_proxy


def apply_user_allocations(
    *,
    proxies: list,
    proxy_groups: list,
    user_allocations: Optional[dict],
    enabled_subs: list[dict],
    node_pool_member_keys: set,
    is_name_allocated: Callable,
) -> tuple[list, list]:
    """Filter proxies by user allocations and regenerate the country groups."""
    # Filter proxies based on user allocations (specific nodes)
    if user_allocations is not None:
        source_allocations = {}
        for sub in enabled_subs:
            alloc_list = user_allocations.get(sub["id"])
            if alloc_list:
                source_allocations[sub["id"]] = alloc_list

        alloc_custom = user_allocations.get("custom_nodes")
        if alloc_custom:
            source_allocations["custom_nodes"] = alloc_custom

        proxies = [
            p
            for p in proxies
            if is_allocated_proxy(
                p,
                source_allocations=source_allocations,
                node_pool_member_keys=node_pool_member_keys,
                is_name_allocated=is_name_allocated,
            )
        ]

        # Regenerate proxy groups based on filtered proxies
        from services.country_grouper import CountryGrouper

        country_groups = CountryGrouper.group_by_country(proxies)
        proxy_groups = ProxyGroupGenerator.generate_groups(proxies, country_groups)

    return proxies, proxy_groups


def apply_template_proxy_groups(
    *,
    template_proxy_groups: Optional[list],
    proxies: list,
    proxy_groups: list,
    filter_underscore_fields: Callable,
) -> list:
    """Apply template-declared proxy groups on top of the generated ones."""
    # If using custom template with proxy-groups, process user config
    if not (template_proxy_groups and isinstance(template_proxy_groups, list) and len(template_proxy_groups) > 0):
        return proxy_groups

    # Get all proxy names
    all_proxy_names = [p["name"] for p in proxies]
    template_group_names = {
        group.get("name") for group in template_proxy_groups if isinstance(group, dict) and group.get("name")
    }

    country_groups = {}
    if any(
        group.get("_imported_clash_group") and "{{COUNTRY_GROUPS}}" in group.get("proxies", [])
        for group in template_proxy_groups
    ):
        country_groups = CountryGrouper.group_by_country(proxies)

    custom_groups = []
    for group in template_proxy_groups:
        new_group = dict(group)

        # Remove underscore fields
        new_group = filter_underscore_fields(new_group)

        original_proxies = group.get("proxies", [])
        if group.get("_imported_clash_group"):
            builtins = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE"}
            valid_names = builtins | template_group_names | set(all_proxy_names)
            references = []
            for name in original_proxies:
                if name == "{{ALL_PROXIES}}":
                    references.extend(all_proxy_names)
                elif name == "{{COUNTRY_GROUPS}}":
                    references.extend(
                        country for country in country_groups if country != group["name"]
                    )
                elif name in valid_names:
                    references.append(name)

            uses_dynamic_nodes = any(group.get(key) for key in (
                "use", "include-all", "include-all-proxies", "include-all-providers",
            ))
            if not references and not uses_dynamic_nodes:
                references = all_proxy_names or ["DIRECT"]
            if references or "proxies" in group:
                new_group["proxies"] = list(dict.fromkeys(references))
        else:
            references = [
                item for item in original_proxies if item in ["DIRECT", "REJECT"] or item in template_group_names
            ]
            new_group["proxies"] = list(dict.fromkeys(references + all_proxy_names))

        custom_groups.append(new_group)

    custom_groups.extend(
        {"name": country, "type": "select", "proxies": nodes}
        for country, nodes in country_groups.items()
        if nodes and country not in template_group_names
    )
    return custom_groups
