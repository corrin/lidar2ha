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
 *   camera   <name>   <x> <y> <z> <yawDeg> <pitchDeg> <fovDeg>
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
  private boolean cameraSet;

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
            case "camera":   setCamera(home, f);   break;
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

    // Only frame here if the scene did not say where to stand. The Python side
    // solves this properly -- eight bounding-box corners against the frustum,
    // with the render's aspect ratio, which this side cannot know -- so a
    // `camera` record always wins. The fallback exists for a hand-written scene
    // file; examples/minimal.tsv is exactly that.
    if (!cameraSet) {
      frameCamera(home);
    }
    return home;
  }

  /**
   * Place the camera exactly where the scene file says.
   *
   * The scene's coordinates are already Sweet Home 3D's -- y flipped and
   * re-origined -- so nothing is transformed here. Angles arrive in degrees
   * because a scene file is meant to be readable.
   */
  private void setCamera(Home home, String[] f) {
    // name, x, y, z, yawDeg, pitchDeg, fovDeg
    Camera camera = home.getTopCamera();
    camera.setX(num(f[2]));
    camera.setY(num(f[3]));
    camera.setZ(num(f[4]));
    camera.setYaw((float) Math.toRadians(num(f[5])));
    camera.setPitch((float) Math.toRadians(num(f[6])));
    camera.setFieldOfView((float) Math.toRadians(num(f[7])));
    camera.setName(f[1]);
    home.setCamera(camera);
    // Also stored, so the view is reachable from Sweet Home 3D's own camera
    // menu and a human can check the framing without rendering anything.
    home.setStoredCameras(java.util.Collections.singletonList(camera.clone()));
    cameraSet = true;
  }

  /**
   * A crude fallback for a scene file that carries no `camera` record.
   *
   * This is NOT the framing calculation -- that is in scan2ha.camera, where it
   * solves the eight bounding-box corners against the frustum and can be tested
   * without a JVM. It cannot live here, because the tight axis depends on the
   * render's aspect ratio and a .sh3d is written long before anyone picks a
   * resolution.
   *
   * So this deliberately errs far back rather than pretending to be exact: a
   * hand-written scene should open showing the whole house, and a small picture
   * of the building beats a large picture of one wall.
   */
  private void frameCamera(Home home) {
    float minX = Float.MAX_VALUE, maxX = -Float.MAX_VALUE;
    float minY = Float.MAX_VALUE, maxY = -Float.MAX_VALUE;
    for (Wall w : home.getWalls()) {
      minX = Math.min(minX, Math.min(w.getXStart(), w.getXEnd()));
      maxX = Math.max(maxX, Math.max(w.getXStart(), w.getXEnd()));
      minY = Math.min(minY, Math.min(w.getYStart(), w.getYEnd()));
      maxY = Math.max(maxY, Math.max(w.getYStart(), w.getYEnd()));
    }
    // Rooms too, not just walls. An open-plan volume is a room polygon with no
    // wall along its open edge -- framing on walls alone would cut it off, and
    // that is exactly the geometry this writer exists to support.
    for (Room room : home.getRooms()) {
      for (float[] point : room.getPoints()) {
        minX = Math.min(minX, point[0]);
        maxX = Math.max(maxX, point[0]);
        minY = Math.min(minY, point[1]);
        maxY = Math.max(maxY, point[1]);
      }
    }
    if (minX > maxX) {
      return;
    }

    // The building's height, which for a multi-level home is most of what has
    // to fit in frame. Sizing the shot from the plan alone put the top floor
    // out of the picture.
    float top = wallHeight;
    for (Level level : home.getLevels()) {
      top = Math.max(top, level.getElevation() + level.getHeight());
    }

    float cx = (minX + maxX) / 2, cy = (minY + maxY) / 2, cz = top / 2;
    float radius = (float) Math.hypot(Math.hypot(maxX - minX, maxY - minY) / 2, cz);

    Camera camera = home.getTopCamera();
    float fov = camera.getFieldOfView();
    // A sphere around the building, then generous slack for the aspect ratio
    // this side cannot see. Over-wide is the safe direction to be wrong in.
    float distance = (float) (radius / Math.sin(fov / 2) * 2.0);
    float pitch = (float) Math.toRadians(50);

    // Aimed at the middle of the building, not at the ground, so a tall home is
    // centred in frame rather than sitting half above it.
    camera.setX(cx);
    camera.setY(cy + distance * (float) Math.cos(pitch));
    camera.setZ(cz + distance * (float) Math.sin(pitch));
    // Sweet Home 3D's yaw looks along (sin yaw, cos yaw), so yaw=0 looks toward
    // INCREASING y. The camera sits at cy + something, on the far side of the
    // plan, so yaw=0 pointed it away from the house at empty space -- and the
    // render came back a uniform white frame rather than failing. Verified by
    // rendering both: yaw=0 gives one colour, yaw=PI gives a picture.
    camera.setYaw((float) Math.PI);
    // Pitch aims at the centre exactly, because the camera's height and its
    // horizontal offset are both derived from `distance` with the same angle.
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
