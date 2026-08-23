"""Turn a phone LiDAR scan into an interactive 3D floorplan in Home Assistant.

The pipeline is a chain of small stages, each of which can be run on its own:

    Polycam DXF/CSV -> polycam    -> Model
    Model + mesh    -> registration -> Model with per-level registration
    Model + mesh    -> textures_* -> texture images
    Model           -> scene      -> scene.tsv
    scene.tsv       -> Sh3dWriter -> .sh3d          (Java; see javabridge)
    .sh3d           -> HeadlessRender -> renders + floorplan.yaml

`schema` defines the Model that the first four stages pass between them, and is
the only description of that structure -- there is no separate spec to drift
from it.
"""

__version__ = "0.1.0"
