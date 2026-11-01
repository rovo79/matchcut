"""The edit core: frame-native, inspectable edits of a matchcut cut on its canonical timeline.

``model`` holds the context every operation and rule reads (``EditContext``) and ``Refused``; ``ops`` the nine operations; ``rules`` the
proposal-wide checks, the timing report, the change report and ``describe``.  The pipeline engine never imports this package.
"""
