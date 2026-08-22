import com.eteks.sweethome3d.io.DefaultFurnitureCatalog;
import com.eteks.sweethome3d.io.HomeFileRecorder;
import com.eteks.sweethome3d.model.Camera;
import com.eteks.sweethome3d.model.CatalogTexture;
import com.eteks.sweethome3d.model.FurnitureCatalog;
import com.eteks.sweethome3d.model.FurnitureCategory;
import com.eteks.sweethome3d.model.Home;
import com.eteks.sweethome3d.model.HomeLight;
import com.eteks.sweethome3d.model.HomeTexture;
import com.eteks.sweethome3d.model.CatalogPieceOfFurniture;
import com.eteks.sweethome3d.model.Level;
import com.eteks.sweethome3d.model.Light;
import com.eteks.sweethome3d.model.Room;
import com.eteks.sweethome3d.model.Wall;
import com.eteks.sweethome3d.tools.URLContent;

import java.io.BufferedReader;
import java.io.File;
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
 *   home     <name>   <wallHeight>
 *   texture  <id>     <absolutePath>  <widthCm>  <heightCm>
 *   level    <id>     <name>  <elevation>  <floorThickness>  <height>
 *   wall     <level>  <xStart> <yStart> <xEnd> <yEnd> <thickness> <height> [leftTex] [rightTex]
 *   room     <level>  <name>   <floorTex> <ceilTex> <x,y> <x,y> <x,y> ...
 *   light    <level>  <name>   <x> <y> <elevation> <power>
 *
 * Texture fields hold a `texture` record's id, or "-"/empty for none. A texture
 * must be declared before the record that references it.
 *
 * There is no guessing on this side: a wall the scan missed gets whatever id the
 * writer chose to give it -- typically the generic tiled patch -- because the
 * Python side is where that decision has the context to be made.
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
  private final List<Level> levelOrder = new ArrayList<>();
  private final Map<String, HomeTexture> textures = new HashMap<>();
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
            case "texture":  addTexture(f);        break;
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

    // Select the lowest level ONCE, after every add has completed.
    //
    // This must not happen during parsing. Home.addWall(), addRoom() and
    // addPieceOfFurniture() all stamp the object with the home's SELECTED
    // level, so a selected level that moved as levels were declared would
    // contaminate everything added after it -- which is exactly the bug the
    // add-then-setLevel ordering below exists to avoid. Doing it here also
    // means Sweet Home 3D opens the file on the ground floor.
    home.setSelectedLevel(levelOrder.get(0));

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

  /** Strip blanks, comments and a leading BOM; split on tabs. */
  private static List<String[]> readRecords(String path) throws Exception {
    List<String[]> records = new ArrayList<>();
    try (BufferedReader in = new BufferedReader(new FileReader(path))) {
      String line;
      while ((line = in.readLine()) != null) {
        // A UTF-8 BOM would otherwise make the first field "\uFEFFhome" and the
        // whole file unreadable -- Windows editors add one without asking, and
        // the failure surfaces as the useless "unknown record type".
        if (!line.isEmpty() && line.charAt(0) == '\uFEFF') {
          line = line.substring(1);
        }
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

  /**
   * Register an image under an id, at a stated physical size.
   *
   * HomeFileRecorder copies the referenced image into the .sh3d, so the finished
   * file does not depend on these paths surviving.
   *
   * The size is what decides tiling: give a texture the exact dimensions of the
   * surface it covers and it appears once, which is how a per-wall rectified
   * photo stays a photo instead of becoming wallpaper. Give it a tile size and
   * it repeats. Both cases are this one record -- the Python side knows each
   * wall's length and height, so it does the arithmetic.
   */
  private void addTexture(String[] f) throws Exception {
    // id, absolutePath, widthCm, heightCm
    File image = new File(f[2]);
    if (!image.isFile()) {
      throw new IllegalArgumentException("texture image not found: " + image);
    }
    URLContent content = new URLContent(image.toURI().toURL());
    CatalogTexture catalog = new CatalogTexture(f[1], content, num(f[3]), num(f[4]));
    textures.put(f[1], new HomeTexture(catalog));
  }

  private void addLevel(Home home, String[] f) {
    // id, name, elevation, floorThickness, height
    Level level = new Level(f[2], num(f[3]), num(f[4]), num(f[5]));
    home.addLevel(level);
    levels.put(f[1], level);
    levelOrder.add(level);
    // NOTE: deliberately no setSelectedLevel() here -- see build().
  }

  private void addWall(Home home, String[] f) {
    // level, xStart, yStart, xEnd, yEnd, thickness, height, [leftTex], [rightTex]
    float height = f.length > 7 && !f[7].isEmpty() ? num(f[7]) : DEFAULT_WALL_HEIGHT;
    Wall wall = new Wall(num(f[2]), num(f[3]), num(f[4]), num(f[5]), num(f[6]), height);

    // Order matters: Home.addWall() stamps the wall with the home's SELECTED
    // level, discarding anything set beforehand. Assign after.
    home.addWall(wall);
    wall.setLevel(level(f[1]));

    HomeTexture left = texture(f, 8);
    HomeTexture right = texture(f, 9);
    if (left != null) {
      wall.setLeftSideTexture(left);
    }
    if (right != null) {
      wall.setRightSideTexture(right);
    }
  }

  private void addRoom(Home home, String[] f) {
    // level, name, floorTex, ceilTex, then one "x,y" per remaining field
    List<float[]> pts = new ArrayList<>();
    for (int i = 5; i < f.length; i++) {
      String[] xy = f[i].split(",");
      pts.add(new float[] {num(xy[0]), num(xy[1])});
    }
    if (pts.size() < 3) {
      throw new IllegalArgumentException("room needs at least 3 points");
    }
    // The Room object is not decoration: the plugin groups light entities by
    // room, so Room Overlay mode depends on it. Room polygons need no enclosing
    // walls, which is what makes open-plan work.
    Room room = new Room(pts.toArray(new float[0][]));
    room.setName(f[2]);
    room.setAreaVisible(false);

    // Same trap as walls: addRoom() stamps the selected level, so the level has
    // to be assigned afterwards.
    home.addRoom(room);
    room.setLevel(level(f[1]));

    HomeTexture floor = texture(f, 3);
    if (floor != null) {
      room.setFloorTexture(floor);
      room.setFloorVisible(true);
    }
    HomeTexture ceiling = texture(f, 4);
    if (ceiling != null) {
      room.setCeilingTexture(ceiling);
      room.setCeilingVisible(true);
    } else {
      // Off unless a ceiling was actually supplied: a ceiling on the level being
      // looked down at occludes the plan the plugin is trying to render.
      room.setCeilingVisible(false);
    }
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
    // The plugin ignores a light source that is not visible or has no power.
    light.setVisible(true);

    // Same trap once more. This is the one that bites hardest: a light on the
    // wrong floor still renders, it just lights the wrong room.
    home.addPieceOfFurniture(light);
    light.setLevel(level(f[1]));
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

  /** Optional texture reference at field `i`; "-" and empty both mean none. */
  private HomeTexture texture(String[] f, int i) {
    if (f.length <= i) {
      return null;
    }
    String id = f[i].trim();
    if (id.isEmpty() || "-".equals(id)) {
      return null;
    }
    HomeTexture texture = textures.get(id);
    if (texture == null) {
      throw new IllegalArgumentException("unknown texture id: " + id);
    }
    return texture;
  }

  private static float num(String s) {
    return Float.parseFloat(s.trim());
  }
}
