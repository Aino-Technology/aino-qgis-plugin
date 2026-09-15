def exec_dialog(dialog):
    """Execute a dialog on both Qt 5 and Qt 6."""
    exec_method = getattr(dialog, "exec", None)
    if exec_method is None:
        exec_method = getattr(dialog, "exec_")
    return exec_method()
