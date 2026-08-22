import com.eteks.sweethome3d.io.DefaultFurnitureCatalog;
import com.eteks.sweethome3d.j3d.ModelManager;
import com.eteks.sweethome3d.model.CatalogPieceOfFurniture;
import com.eteks.sweethome3d.model.FurnitureCatalog;
import com.eteks.sweethome3d.model.FurnitureCategory;
import com.eteks.sweethome3d.model.Light;

import javax.media.j3d.BranchGroup;
import javax.media.j3d.Geometry;
import javax.media.j3d.Group;
import javax.media.j3d.IndexedTriangleArray;
import javax.media.j3d.Node;
import javax.media.j3d.Shape3D;
import javax.media.j3d.TriangleArray;
import java.util.Enumeration;

/**
 * List every Light in the default furniture catalog with its id and whether its
 * 3D model contains real surface geometry.
 *
 * Sh3dWriter originally took the FIRST Light it found, which turned out to have
 * line-only geometry -- so it rendered as a glow with no visible fixture and
 * exported to OBJ with no faces at all. Pick a light by id instead.
 */
public class CatalogProbe {
  public static void main(String[] args) {
    FurnitureCatalog catalog = new DefaultFurnitureCatalog();
    for (FurnitureCategory category : catalog.getCategories()) {
      for (CatalogPieceOfFurniture piece : category.getFurniture()) {
        if (!(piece instanceof Light)) {
          continue;
        }
        String kinds = "unloadable";
        try {
          BranchGroup model = ModelManager.getInstance().loadModel(piece.getModel());
          kinds = geometryKinds(model);
        } catch (Exception e) {
          kinds = "ERROR " + e.getClass().getSimpleName();
        }
        System.out.printf("%-40s %-28s %s%n", piece.getId(), category.getName(), kinds);
      }
    }
  }

  /** Distinct Geometry subclasses in the tree -- surfaces vs lines. */
  private static String geometryKinds(Node node) {
    StringBuilder sb = new StringBuilder();
    collect(node, sb);
    return sb.length() == 0 ? "(no shapes)" : sb.toString();
  }

  private static void collect(Node node, StringBuilder sb) {
    if (node instanceof Shape3D) {
      Shape3D s = (Shape3D) node;
      for (int i = 0; i < s.numGeometries(); i++) {
        Geometry g = s.getGeometry(i);
        if (g == null) {
          continue;
        }
        String n = g.getClass().getSimpleName();
        boolean surface = g instanceof TriangleArray || g instanceof IndexedTriangleArray
            || n.contains("Triangle") || n.contains("Quad");
        String tag = (surface ? "SURFACE:" : "line:") + n;
        if (sb.indexOf(tag) < 0) {
          sb.append(sb.length() > 0 ? " " : "").append(tag);
        }
      }
    }
    if (node instanceof Group) {
      Enumeration<?> e = ((Group) node).getAllChildren();
      while (e.hasMoreElements()) {
        collect((Node) e.nextElement(), sb);
      }
    }
  }
}
