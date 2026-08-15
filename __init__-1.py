# -*- coding: utf-8 -*-


def classFactory(iface):
    from .layer_tree_bridge_fix import BridgeSafeInreach2QGISPlugin
    return BridgeSafeInreach2QGISPlugin(iface)
