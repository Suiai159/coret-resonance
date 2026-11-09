"""One light palette for widgets, popup windows and dialogs.

Fusion selects widget geometry, not a light color scheme. Never combine its
inherited system text colors with the window's fixed white backgrounds.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette


def apply_theme(app):
    """Apply before constructing any widgets; leave the OS theme unchanged."""
    app.setStyle('Fusion')
    app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    palette = QPalette()
    colors = {
        'Window': '#f2f5f8', 'WindowText': '#243746',
        'Base': '#ffffff', 'AlternateBase': '#edf2f6', 'Text': '#243746',
        'Button': '#ffffff', 'ButtonText': '#243746',
        'Light': '#ffffff', 'Midlight': '#edf2f6', 'Mid': '#cbd6df',
        'Dark': '#697c8a', 'Shadow': '#334d60', 'BrightText': '#ffffff',
        'Highlight': '#167c72', 'HighlightedText': '#ffffff',
        'ToolTipBase': '#fffbea', 'ToolTipText': '#243746',
        'Link': '#126c63', 'LinkVisited': '#67549c',
        'PlaceholderText': '#697c8a', 'Accent': '#167c72',
    }
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive,
                  QPalette.ColorGroup.Disabled):
        for role, color in colors.items():
            palette.setColor(group, getattr(QPalette.ColorRole, role), QColor(color))
    for role in ('WindowText', 'Text', 'ButtonText', 'PlaceholderText'):
        palette.setColor(QPalette.ColorGroup.Disabled,
                         getattr(QPalette.ColorRole, role), QColor('#697c8a'))
    for role in ('Base', 'Button'):
        palette.setColor(QPalette.ColorGroup.Disabled,
                         getattr(QPalette.ColorRole, role), QColor('#f2f4f7'))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Highlight,
                     QColor('#d8e3e8'))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.HighlightedText,
                     QColor('#425b6a'))
    app.setPalette(palette)
