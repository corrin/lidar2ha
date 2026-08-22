import com.eteks.sweethome3d.io.DefaultFurnitureCatalog;
import com.eteks.sweethome3d.io.HomeFileRecorder;
import com.eteks.sweethome3d.model.Camera;
import com.eteks.sweethome3d.model.FurnitureCatalog;
import com.eteks.sweethome3d.model.FurnitureCategory;
import com.eteks.sweethome3d.model.Home;
import com.eteks.sweethome3d.model.HomeLight;
import com.eteks.sweethome3d.model.HomePieceOfFurniture;
import com.eteks.sweethome3d.model.CatalogPieceOfFurniture;
import com.eteks.sweethome3d.model.Level;
import com.eteks.sweethome3d.model.Light;
import com.eteks.sweethome3d.model.Room;
import com.eteks.sweethome3d.model.Wall;

import java.io.BufferedReader;
import java.io.FileReader;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Write a Sweet Home 3D .sh3d file from a plain-text scene description.
 *
 * WHY THIS EXISTS
 * ---------------
 * A .sh3d is a ZIP, and since Sweet Home 3D 5.3 it contains a Home.xml entry
 * conforming to SweetHome3D.dtd. That makes it tempting to generate one by
 * writing XML and zipping it. Sweet Home 3D will refuse to open the result
 * ("can't open home"): the desktop application reads the *Java-serialised*
 * `Home` entry, and Home.xml is there for other tools. Dumping an archive
 * written by Sweet Home 3D itself shows a single entry named `Home`.
 *
 * Java serialisation of Sweet Home 3D's own classes can only be produced by
 * those classes, so any generator has to go through them. This program is that
 * bridge: parse a simple description, build real model objects, and hand them
 * to HomeFileRecorder.
 *
 * UNITS ARE CENTIMETRES. Sweet Home 3D's internal unit. Most scanning apps
 * export millimetres -- divide by 10 before you get here.
 *
 * INPUT FORMAT
 * ------------
 * Tab-separated, one record per line. Blank lines and lines starting with '#'
 * are ignored. A deliberately dumb format so the Java side needs no JSON
 * dependency; the Python side does the real parsing.
 *
 *   home   <name>  <wallHeight>
 *   level  <id>    <name>  <elevation>  <floorThickness>  <height>
 *   wall   <level> <xStart> <yStart> <xEnd> <yEnd> <thickness> <height>
 *   room   <level> <name>  <x,y> <x,y> <x,y> ...
 *   light  <level> <name>  <x> <y> <elevation> <power>
 *
 * `name` on a light (or piece) must be the Home Assistant entity_id exactly --
 * that is what the home-assistant-floor-plan plugin matches on.
 *
 * USAGE
 *   javac -cp SweetHome3D.jar -d out Sh3dWriter.java
 *   java  -cp "SweetHome3D.jar;Furniture.jar;out" Sh3dWriter scene.tsv out.sh3d
 *
 * Furniture.jar must be on the classpath for lights: a HomeLight is built from
 * a catalog light, which supplies the 3D model and the light source geometry.
 */
public class Sh3dWriter {

  private static final float DEFAULT_WALL_HEIGHT = 250f;

  public static void main(String[] args) throws Exception {
    if (args.length < 2) {
      System.err.println("usage: Sh3dWriter <scene.tsv> <out.sh3d>");
      System.exit(2);
    }
    Home home = new Sh3dWriter().build(args[0]);
    new HomeFileRecorder().writeHome(home, args[1]);
    System.out.println("wrote " + args[1]);
  }

  private final Map<String, Level> levels = new HashMap<>();
  private float wallHeight = DEFAULT_WALL_HEIGHT;
  private Light catalogLight;

  Home build(String scenePath) throws Exception {
    List<String[]> records = readRecords(scenePath);

    // Home's default wall height is constructor-only -- there is no setter --
    // so the `home` record has to be found before anything is built.
    wallHeight = defaultWallHeight(records);
    Home home = new Home(wallHeight);
    int lineNo = 0;

    {
      for (String[] f : records) {
        lineNo++;
        try {
          switch (f[0]) {
            case "home":     applyHome(home, f);   break;
            case "level":    addLevel(home, f);    break;
            case "wall":     addWall(home, f);     break;
            case "room":     addRoom(home, f);     break;
            case "light":    addLight(home, f);    break;
            default:
              throw new IllegalArgumentException("unknown record type: " + f[0]);
          }
        } catch (RuntimeException e) {
          throw new IllegalArgumentException(
              scenePath + " record " + lineNo + ": " + e.getMessage(), e);
        }
      }
    }

    if (home.getLevels().isEmpty()) {
      throw new IllegalStateException("scene declared no levels");
    }
    frameCamera(home);
    return home;
  }

  /**
   * Point the top camera at the model.
   *
   * Sweet Home 3D frames the camera interactively; headless there is nothing to
   * do it, so a generated home renders from wherever the default camera happens
   * to sit -- usually with the building half out of frame. Compute the plan
   * bounds and pull the camera back far enough to contain them.
   */
  private void frameCamera(Home home) {
    if (home.getWalls().isEmpty()) {
      return;
    }
    float minX = Float.MAX_VALUE, maxX = -Float.MAX_VALUE;
    float minY = Float.MAX_VALUE, maxY = -Float.MAX_VALUE;
    for (Wall w : home.getWalls()) {
      minX = Math.min(minX, Math.min(w.getXStart(), w.getXEnd()));
      maxX = Math.max(maxX, Math.max(w.getXStart(), w.getXEnd()));
      minY = Math.min(minY, Math.min(w.getYStart(), w.getYEnd()));
      maxY = Math.max(maxY, Math.max(w.getYStart(), w.getYEnd()));
    }
    float cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
    float radius = (float) Math.hypot(maxX - minX, maxY - minY) / 2;

    Camera camera = home.getTopCamera();
    float fov = camera.getFieldOfView();
    // Pull back so the bounding circle fits, with headroom for wall height.
    float distance = (float) (radius / Math.tan(fov / 2) * 1.35) + wallHeight;
    float pitch = (float) Math.toRadians(50);

    camera.setX(cx);
    camera.setY(cy + distance * (float) Math.cos(pitch));
    camera.setZ(distance * (float) Math.sin(pitch));
    camera.setYaw(0f);
    camera.setPitch(pitch);
    home.setCamera(camera);
  }

  /** Strip blanks and comments; split on tabs. */
  private static List<String[]> readRecords(String path) throws Exception {
    List<String[]> records = new ArrayList<>();
    try (BufferedReader in = new BufferedReader(new FileReader(path))) {
      String line;
      while ((line = in.readLine()) != null) {
        line = line.trim();
        if (!line.isEmpty() && !line.startsWith("#")) {
          records.add(line.split("\t"));
        }
      }
    }
    return records;
  }

  private static float defaultWallHeight(List<String[]> records) {
    for (String[] f : records) {
      if ("home".equals(f[0]) && f.length > 2) {
        return num(f[2]);
      }
    }
    return DEFAULT_WALL_HEIGHT;
  }

  private void applyHome(Home home, String[] f) {
    home.setName(f[1]);
    // wallHeight was consumed by the constructor -- see defaultWallHeight.
  }

  private void addLevel(Home home, String[] f) {
    // id, name, elevation, floorThickness, height
    Level level = new Level(f[2], num(f[3]), num(f[4]), num(f[5]));
    home.addLevel(level);
    // The last level added wins as selected; harmless for our purposes and
    // means single-level scenes behave sensibly.
    home.setSelectedLevel(level);
    levels.put(f[1], level);
  }

  private void addWall(Home home, String[] f) {
    // level, xStart, yStart, xEnd, yEnd, thickness, height
    float height = f.length > 7 && !f[7].isEmpty() ? num(f[7]) : DEFAULT_WALL_HEIGHT;
    Wall wall = new Wall(num(f[2]), num(f[3]), num(f[4]), num(f[5]), num(f[6]), height);
    wall.setLevel(level(f[1]));
    home.addWall(wall);
  }

  private void addRoom(Home home, String[] f) {
    // level, name, then one "x,y" per remaining field
    List<float[]> pts = new ArrayList<>();
    for (int i = 3; i < f.length; i++) {
      String[] xy = f[i].split(",");
      pts.add(new float[] {num(xy[0]), num(xy[1])});
    }
    if (pts.size() < 3) {
      throw new IllegalArgumentException("room needs at least 3 points");
    }
    Room room = new Room(pts.toArray(new float[0][]));
    room.setName(f[2]);
    room.setLevel(level(f[1]));
    room.setAreaVisible(false);
    home.addRoom(room);
  }

  private void addLight(Home home, String[] f) throws Exception {
    // level, name, x, y, elevation, power
    HomeLight light = new HomeLight(catalogLight());
    // The plugin matches on the object's NAME. This is the whole mapping.
    light.setName(f[2]);
    light.setX(num(f[3]));
    light.setY(num(f[4]));
    light.setElevation(num(f[5]));
    if (f.length > 6 && !f[6].isEmpty()) {
      light.setPower(num(f[6]));
    }
    light.setLevel(level(f[1]));
    home.addPieceOfFurniture(light);
  }

  /**
   * Catalog light used as the template for every generated fixture.
   *
   * Chosen BY ID deliberately. The catalog's `*LightSource` entries (blueLightSource,
   * halogenLightSource, lightSource, ...) are invisible emitters whose "model" is
   * line geometry -- a marker, not a lamp. Taking the first Light in the catalog
   * picks `eTeks#blueLightSource` alphabetically, which emits light correctly but
   * exports to OBJ with **no faces at all**, so the fixture is invisible in any
   * mesh-based viewer. Fixtures with real surfaces: pendantLamp, spotlight,
   * floorUplight, wallUplight, lamp, workLamp.
   */
  private static final String LIGHT_CATALOG_ID =
      System.getProperty("lightCatalogId", "eTeks#pendantLamp");

  private Light catalogLight() {
    if (catalogLight != null) {
      return catalogLight;
    }
    FurnitureCatalog catalog = new DefaultFurnitureCatalog();
    Light fallback = null;
    for (FurnitureCategory category : catalog.getCategories()) {
      for (CatalogPieceOfFurniture piece : category.getFurniture()) {
        if (!(piece instanceof Light)) {
          continue;
        }
        if (LIGHT_CATALOG_ID.equals(piece.getId())) {
          catalogLight = (Light) piece;
          return catalogLight;
        }
        if (fallback == null) {
          fallback = (Light) piece;
        }
      }
    }
    if (fallback == null) {
      throw new IllegalStateException(
          "no light in the furniture catalog -- is Furniture.jar on the classpath?");
    }
    System.err.println("WARNING: light '" + LIGHT_CATALOG_ID
        + "' not found; falling back to " + ((CatalogPieceOfFurniture) fallback).getId()
        + " which may have no surface geometry");
    catalogLight = fallback;
    return catalogLight;
  }

  private Level level(String id) {
    Level level = levels.get(id);
    if (level == null) {
      throw new IllegalArgumentException("unknown level id: " + id);
    }
    return level;
  }

  private static float num(String s) {
    return Float.parseFloat(s.trim());
  }
}
