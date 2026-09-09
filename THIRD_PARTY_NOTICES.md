# Third-Party Notices

## Blender MCP

The integrated MCP bridge, server, tools, and bundled Blender documentation are
adapted from Blender Lab's `blender_mcp` project:

- Source: https://projects.blender.org/lab/blender_mcp
- Vendored revision: `98b0e49d98321d321c7e631389200f513f765d59`
- Copyright: 2026 Blender Authors
- License: GNU General Public License v3.0 or later

Upstream SPDX headers are retained in adapted source files. Runtime Python
source is included in the bundled `blender_mcp` wheel. The complete upstream
source is available from the repository and pinned revision listed above.

Bundled Python wheels retain their own package metadata and license files.

## Segno

The phone-camera QR code shown in the add-on is generated with the `segno`
library (https://github.com/heuer/segno), bundled as an unmodified wheel.
Segno is licensed under the MIT License.

## Blender MCP server

The MCP server the agent drives this Blender through is `blender-mcp`
(`blmcp`), by the Blender Authors, bundled as an unmodified wheel and licensed
under the GPL-3.0-or-later. The add-on's own `mcp_bridge/` package is adapted
from Blender Lab's `blender_mcp_addon` under the same licence.

## Skia

The viewport prompt box is rasterized with `skia-python`
(https://github.com/kyamagu/skia-python), bundled as unmodified wheels that
embed Google's Skia graphics library (https://skia.org). Both `skia-python` and
Skia are licensed under the BSD 3-Clause License.
## Bundled fonts

The overlay UI ships two typefaces under `resources/fonts`, both unmodified
variable TTFs licensed under the SIL Open Font License 1.1:

- Inter — https://github.com/rsms/inter, Copyright 2016 The Inter Project Authors
- Space Grotesk — https://github.com/floriankarsten/space-grotesk,
  Copyright 2020 The Space Grotesk Project Authors

The OFL text is available at https://openfontlicense.org.
