import com.eteks.sweethome3d.io.DefaultUserPreferences;
import com.eteks.sweethome3d.io.HomeFileRecorder;
import com.eteks.sweethome3d.j3d.ModelManager;
import com.eteks.sweethome3d.j3d.OBJWriter;
import com.eteks.sweethome3d.j3d.Object3DBranchFactory;
import com.eteks.sweethome3d.model.Home;
import com.eteks.sweethome3d.model.HomeLight;
import com.eteks.sweethome3d.model.HomePieceOfFurniture;
import com.eteks.sweethome3d.model.Room;
import com.eteks.sweethome3d.model.Selectable;
import com.eteks.sweethome3d.model.UserPreferences;
import com.eteks.sweethome3d.model.Wall;

import javax.media.j3d.Node;
import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.ArrayList;
import java.util.List;

/**
 * Export a .sh3d to Wavefront OBJ/MTL, naming each OBJ group after the Home
 * Assistant entity it represents.
 *
 * WHY THE NAMING MATTERS
 * ----------------------
 * A real-time 3D floor-plan card binds Home Assistant entities to objects in the
 * model *by object name*. OBJWriter.writeNode(node, name) emits that name as the
 * OBJ group. So naming a light's group `light.hallway_ceiling` is the whole
 * mapping -- the same convention the home-assistant-floor-plan plugin already
 * uses, which means one generated model drives both the raytraced
 * picture-elements view and the interactive 3D card.
 *
 * Names are the ONLY thing this file exists to produce. floor3d-card loads OBJ
 * directly; the better-maintained cards want GLB, and the obvious conversion
 * (trimesh) keys geometry by material and replaces every name with a material
 * name, silently. See lidar2ha/glb.py, which converts with obj2gltf and counts
 * how many names survived.
 *
 * Walls and rooms get structural names; they are scenery, not entities.
 *
 * UNITS: Sweet Home 3D works in centimetres, and OBJ is unitless, so the model
 * arrives in the card at centimetre scale.
 *
 * WHICH JVM
 * ---------
 * Sweet Home 3D's OWN 32-bit runtime, exactly as for HeadlessRender -- not the
 * 64-bit JDK this file used to claim. Nothing here raytraces, and the export is
 * pure geometry, but Object3DBranchFactory builds Java3D scene graphs and the
 * 64-bit JDK dies loading their natives:
 *
 *     Can't load IA 32-bit .dll on a AMD 64-bit platform
 *
 * And do NOT set -Djava.awt.headless=true. It is the obvious flag for a
 * command-line export and it is precisely wrong: VirtualUniverse's static
 * initialiser touches the display, so the run dies with HeadlessException
 * before main() -- headless is what breaks it, not what fixes it.
 *
 * That runtime ships only javaw.exe, which has no console, so -DlogFile is how
 * anything here is ever read. Without it this exports in total silence and the
 * only evidence is whether a file appeared.
 *
 *   javaw -DlogFile=x.log -cp "<all sh3d jars>;out" \
 *         -Djava.library.path=<sh3d lib> ObjExport in.sh3d out.obj
 *
 * lidar2ha.javabridge.render_command assembles all of that; `lidar2ha
 * export-glb` is the front door.
 */
public class ObjExport {

  public static void main(String[] args) throws Exception {
    // See the class comment: this runs on a console-less javaw.exe, so stdout
    // goes to a file or nowhere at all. Same bootstrap as HeadlessRender, and
    // for the same reason -- including the uncaught-exception handler, without
    // which a failure is an empty log and a missing file.
    String logFile = System.getProperty("logFile");
    if (logFile != null) {
      java.io.PrintStream log = new java.io.PrintStream(
          new java.io.FileOutputStream(logFile, true), true, "UTF-8");
      System.setOut(log);
      System.setErr(log);
      Thread.setDefaultUncaughtExceptionHandler((t, e) -> {
        e.printStackTrace(log);
        log.flush();
      });
    }

    if (args.length < 2) {
      System.err.println("usage: ObjExport <model.sh3d> <out.obj>");
      System.exit(2);
    }

    UserPreferences prefs = new DefaultUserPreferences();
    Home home = new HomeFileRecorder(9, false, prefs, false, true, true).readHome(args[0]);
    System.out.println("loaded " + args[0]
        + "  levels=" + home.getLevels().size()
        + " walls=" + home.getWalls().size()
        + " rooms=" + home.getRooms().size()
        + " furniture=" + home.getFurniture().size());

    Object3DBranchFactory factory = new Object3DBranchFactory(prefs);
    OBJWriter writer = new OBJWriter(new File(args[1]), "scan2sh3d export", -1);

    int walls = 0, rooms = 0, entities = 0;

    for (Wall wall : home.getWalls()) {
      if (write(writer, factory, home, wall, "wall_" + walls)) {
        walls++;
      }
    }

    for (Room room : home.getRooms()) {
      String name = room.getName() != null
          ? "room_" + room.getName().replaceAll("[^A-Za-z0-9_]", "_")
          : "room_" + rooms;
      if (write(writer, factory, home, room, name)) {
        rooms++;
      }
    }

    for (HomePieceOfFurniture piece : home.getFurniture()) {
      // Furniture 3D models are normally loaded ASYNCHRONOUSLY, so building the
      // node immediately yields an empty branch and the piece exports with no
      // geometry at all -- silently. Warm ModelManager's cache with the
      // blocking loadModel(Content) overload first.
      if (piece.getModel() != null) {
        try {
          ModelManager.getInstance().loadModel(piece.getModel());
        } catch (IOException e) {
          System.err.println("  model load failed for " + piece.getName() + ": " + e);
        }
      }

      // The piece's name IS the entity id for anything the plugin would bind.
      // Keep it verbatim so floor3d-card can match it.
      String name = piece.getName();
      if (name == null || name.isEmpty()) {
        name = "furniture_" + entities;
      }
      if (write(writer, factory, home, piece, name)) {
        entities++;
        System.out.println("    " + (piece instanceof HomeLight ? "LIGHT " : "piece ") + name);
      }
    }

    writer.close();

    // Rewrite group names to entity ids -- see renameGroupsByItem.
    File obj = new File(args[1]);
    renameGroupsByItem(obj);

    System.out.printf("wrote %s (%.1f KB): %d walls, %d rooms, %d furniture%n",
        obj.getPath(), obj.length() / 1024.0, walls, rooms, entities);
  }

  static final String ITEM_MARKER = "# scan2sh3d-item ";

  /**
   * Rewrite every `g` line to the name of the item currently being written, using
   * the markers emitted before each writeNode call. This is what makes the OBJ
   * group equal the Home Assistant entity id exactly -- which is how floor3d-card
   * binds entities to objects.
   */
  private static void renameGroupsByItem(File obj) throws IOException {
    List<String> out = new ArrayList<>();
    String current = null;
    for (String line : Files.readAllLines(obj.toPath(), StandardCharsets.UTF_8)) {
      if (line.startsWith(ITEM_MARKER)) {
        current = line.substring(ITEM_MARKER.length()).trim();
        out.add(line);
      } else if (current != null && line.startsWith("g ")) {
        out.add("g " + current);
      } else {
        out.add(line);
      }
    }
    Files.write(obj.toPath(), out, StandardCharsets.UTF_8);
  }

  /** Build the Java3D branch for one home item and write it as a named group. */
  private static boolean write(OBJWriter writer, Object3DBranchFactory factory,
                               Home home, Selectable item, String name) {
    try {
      Object object3D = factory.createObject3D(home, item, true);
      if (object3D instanceof Node) {
        // OBJWriter names groups after the model's own internal parts when it has
        // them, so a catalog lamp exports as Cable/Socle/Sphere and the entity id
        // never appears -- clearing Node names does not change this. Emit an
        // explicit marker instead and rewrite the group names by range afterwards.
        writer.write("\n" + ITEM_MARKER + name + "\n");
        writer.writeNode((Node) object3D, name);
        return true;
      }
      System.err.println("  skipped (not a Node): " + name);
    } catch (Exception e) {
      System.err.println("  skipped " + name + ": " + e);
    }
    return false;
  }

}
