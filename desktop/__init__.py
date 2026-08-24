"""Optional desktop shell for the AIDrama Studio local web application.

The launcher symbols are exposed lazily so ``python -m desktop.launcher`` does
not import the target module while :mod:`runpy` is preparing it (which would
otherwise emit a warning and make packaged startup diagnostics noisy).
"""

__all__ = ["DesktopLaunchError", "DesktopLauncher", "LauncherConfig"]


def __getattr__(name: str):
    if name in __all__:
        from desktop import launcher

        return getattr(launcher, name)
    raise AttributeError(name)
