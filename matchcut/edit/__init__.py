"""The edit core: frame-native, inspectable edits of a matchcut cut on its canonical timeline.

``model`` holds the context every operation and rule reads (``EditContext``) and ``Refused``; ``ops`` the nine operations; ``rules`` the
proposal-wide checks, the timing report, the change report and ``describe``; ``session`` the store ``WS/edits/`` (revisions, proposals,
apply, undo/redo/checkout, compare, staleness and the lock).  The pipeline engine never imports this package.
"""
