Noto Sans Bengali + Latin (bundled fonts)
=========================================

Files
-----
NotoSansBengaliLatin-Regular.ttf
NotoSansBengaliLatin-Bold.ttf

Purpose
-------
These are single merged font files used by the PDF fallback renderer
(fpdf2) and by the headless-browser PDF template. Keeping Bengali and
Latin glyphs in one file avoids a glyph-subsetting bug that appeared
when fpdf2 used two fallback fonts for a single mixed-script string
(one totals label rendered corrupted).

Source
------
Generated from Google Fonts "Noto Sans Bengali" (variable, wght 100-900)
and its matching Latin subset. Instances at wght 400 and 700 were merged
with fontTools.merge.

License
-------
Noto fonts are licensed under the SIL Open Font License, Version 1.1.
https://openfontlicense.org
https://github.com/notofonts/bengali

The OFL permits bundling and redistribution with this application.
The fonts are used unmodified in glyph content (merged only); the
reserved font names are not claimed by this project.

Regenerating
------------
1. Download the Noto Sans Bengali variable woff2 (Bengali + Latin ranges)
   from fonts.gstatic.com.
2. Instantiate wght 400 and 700 with fontTools.varLib.instancer.
3. Merge each weight's Bengali and Latin instance with fontTools.merge.
4. Save as NotoSansBengaliLatin-{Regular,Bold}.ttf in this directory.
