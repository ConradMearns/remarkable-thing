#import "test06.ink/overlay.typ": ink
#show: ink
#set page(width: 157.8mm, height: 210.4mm, margin: 15mm)
#set text(size: 12pt)

= PDF path test

This document was compiled from Typst to PDF on the laptop and pushed as a PDF.
Annotate it on the tablet; `rmsync pdf pull` exports the ink per page as SVG overlays.

$ integral_0^1 x^3 dif x = 1/4 $

#table(columns: 3, [a], [b], [c], [1], [2], [3])

#pagebreak()

= Page two

Draw something here too.
