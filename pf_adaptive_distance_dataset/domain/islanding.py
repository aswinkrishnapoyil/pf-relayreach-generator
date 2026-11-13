# islanding.py
"""
Islanding check for switch states.

A switch state can split the grid so that some busbars are no longer
connected to any external grid (ElmXnet). The distance-protection setting
rules assume a supplied network, so relays in such an island would receive
settings that have no physical meaning (no fault current from the grid side,
no infeed correction). The generator therefore omits switch states that
create an island.

The core of the check, ``find_unsupplied_islands``, is plain Python and does
not need PowerFactory. ``find_live_unsupplied_islands`` collects the graph
from the active PowerFactory case and passes it to the core function.
"""
from __future__ import annotations

from typing import Hashable, Iterable

from ..core.config import PFAttr
from ..pf_api.pf_utils import (
    get_pf_attribute,
    get_safe_full_name,
    get_safe_name,
    is_object_in_service,
    is_object_inside_grid,
)
from .topology import (
    get_cubicle_switch_closed_state,
    get_line_is_available_for_topology,
    get_terminal_from_cubicle,
)

ISLANDED_SWITCH_STATE_REASON = "islanded_switch_state"


def find_unsupplied_islands(
        edges: Iterable[tuple[Hashable, Hashable]],
        source_nodes: Iterable[Hashable],
        checked_nodes: Iterable[Hashable] | None = None,
) -> list[list[Hashable]]:
    """
    Returns the connected groups of nodes that contain no source node.

    Args:
        edges: Closed connections (node_a, node_b). Only in-service elements
            with closed switches at both ends should be passed in.
        source_nodes: Nodes with an in-service external grid.
        checked_nodes: Nodes that must be supplied. Defaults to every node
            that appears in ``edges``. Groups that contain none of these
            nodes are ignored.

    Returns:
        A list of islands. Each island lists the checked nodes it contains,
        sorted by their string form. The islands are sorted by their first
        node. An empty list means every checked node is supplied.
    """

    d_parent: dict[Hashable, Hashable] = {}

    def _find(node):
        d_parent.setdefault(node, node)
        o_root = node
        while d_parent[o_root] != o_root:
            o_root = d_parent[o_root]
        while d_parent[node] != o_root:
            d_parent[node], node = o_root, d_parent[node]
        return o_root

    def _union(node_a, node_b):
        o_root_a = _find(node_a)
        o_root_b = _find(node_b)
        if o_root_a != o_root_b:
            d_parent[o_root_b] = o_root_a

    l_edge_nodes = []
    for o_node_a, o_node_b in edges:
        _union(o_node_a, o_node_b)
        l_edge_nodes.extend((o_node_a, o_node_b))

    s_supplied_roots = {_find(o_node) for o_node in source_nodes}

    l_checked = list(l_edge_nodes if checked_nodes is None else checked_nodes)

    d_islands: dict[Hashable, set] = {}
    for o_node in l_checked:
        o_root = _find(o_node)
        if o_root not in s_supplied_roots:
            d_islands.setdefault(o_root, set()).add(o_node)

    l_islands = [
        sorted(s_nodes, key=str)
        for s_nodes in d_islands.values()
    ]
    l_islands.sort(key=lambda l_nodes: str(l_nodes[0]))
    return l_islands


def _get_closed_terminals(element, cubicle_attributes):
    """
    Returns the terminals of an element whose cubicle switches are closed.

    Returns None if the element is out of service. Terminals behind an open
    switch are left out, so the element only connects the remaining ones.
    """

    if element is None or not is_object_in_service(element):
        return None

    l_terminals = []
    for s_attribute in cubicle_attributes:
        o_cubicle = get_pf_attribute(element, s_attribute)
        if o_cubicle is None:
            continue
        if get_cubicle_switch_closed_state(o_cubicle) != 1:
            continue
        o_terminal = get_terminal_from_cubicle(o_cubicle)
        if o_terminal is not None:
            l_terminals.append(o_terminal)
    return l_terminals


def collect_live_supply_graph(grid, app):
    """
    Collects the supply graph of the active PowerFactory case.

    Edges are in-service lines with both switches closed, closed in-service
    couplers (ElmCoup) and in-service transformers (ElmTr2, ElmTr3) between
    their connected sides. Elements outside the selected grid are included,
    so an external grid behind a transformer still counts as a source.

    Returns:
        dict with
            - edges: list of (terminal_key, terminal_key)
            - sources: set of terminal keys with an in-service external grid
            - checked: set of terminal keys of the traced grid, i.e. the
              ends of available lines inside the selected grid
            - names: terminal key -> readable terminal name
    """

    l_edges = []
    s_sources = set()
    s_checked = set()
    d_names = {}

    def _key(terminal):
        s_key = get_safe_full_name(terminal)
        d_names.setdefault(s_key, get_safe_name(terminal))
        return s_key

    for o_line in app.GetCalcRelevantObjects("*.ElmLne") or []:
        if not get_line_is_available_for_topology(o_line):
            continue
        o_from = get_terminal_from_cubicle(get_pf_attribute(o_line, PFAttr.BUS1))
        o_to = get_terminal_from_cubicle(get_pf_attribute(o_line, PFAttr.BUS2))
        if o_from is None or o_to is None:
            continue
        l_edges.append((_key(o_from), _key(o_to)))
        if is_object_inside_grid(o_from, grid) and is_object_inside_grid(o_to, grid):
            s_checked.update((_key(o_from), _key(o_to)))

    for o_coupler in app.GetCalcRelevantObjects("*.ElmCoup") or []:
        if get_pf_attribute(o_coupler, PFAttr.SWITCH_STATE, 1, int) != 1:
            continue
        l_terminals = _get_closed_terminals(o_coupler, (PFAttr.BUS1, PFAttr.BUS2))
        if l_terminals and len(l_terminals) == 2:
            l_edges.append((_key(l_terminals[0]), _key(l_terminals[1])))

    for s_filter, t_attributes in (
            ("*.ElmTr2", (PFAttr.BUSHV, PFAttr.BUSLV)),
            ("*.ElmTr3", (PFAttr.BUSHV, PFAttr.BUSMV, PFAttr.BUSLV)),
    ):
        for o_transformer in app.GetCalcRelevantObjects(s_filter) or []:
            l_terminals = _get_closed_terminals(o_transformer, t_attributes)
            if not l_terminals:
                continue
            for o_terminal in l_terminals[1:]:
                l_edges.append((_key(l_terminals[0]), _key(o_terminal)))

    for o_xnet in app.GetCalcRelevantObjects("*.ElmXnet") or []:
        l_terminals = _get_closed_terminals(o_xnet, (PFAttr.BUS1,))
        for o_terminal in l_terminals or []:
            s_sources.add(_key(o_terminal))

    return {
        "edges": l_edges,
        "sources": s_sources,
        "checked": s_checked,
        "names": d_names,
    }


def find_live_unsupplied_islands(grid, app) -> list[list[str]]:
    """
    Returns the unsupplied islands of the active case as lists of terminal
    names. An empty list means the traced grid is fully supplied.
    """

    d_graph = collect_live_supply_graph(grid, app)
    l_islands = find_unsupplied_islands(
        d_graph["edges"],
        d_graph["sources"],
        d_graph["checked"],
    )
    d_names = d_graph["names"]
    return [
        sorted(d_names.get(s_key, s_key) for s_key in l_island)
        for l_island in l_islands
    ]


def format_islands(islands: list[list[str]]) -> str:
    """Formats islands for logs and omission records."""

    return "; ".join(
        "[" + ", ".join(str(s_node) for s_node in l_island) + "]"
        for l_island in islands
    )
