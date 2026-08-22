import com.eteks.sweethome3d.io.HomeFileRecorder;
import com.eteks.sweethome3d.model.Home;
import com.eteks.sweethome3d.model.HomeLight;
import com.eteks.sweethome3d.model.HomePieceOfFurniture;
import com.eteks.sweethome3d.model.HomeTexture;
import com.eteks.sweethome3d.model.Level;
import com.eteks.sweethome3d.model.Room;
import com.eteks.sweethome3d.model.Wall;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Read a .sh3d back through HomeFileRecorder and report what is inside.
 *
 * This is the same code path Sweet Home 3D uses to open a file, so a clean run
 * here means the archive is loadable -- as opposed to merely *looking* like a
 * valid archive. Writing a file that structurally resembled a real one but
 * could not be opened is exactly the trap this exists to catch.
 *
 * EVERY OBJECT REPORTS ITS LEVEL. That is not decoration. Home.addWall(),
 * addRoom() and addPieceOfFurniture() overwrite an object's level with the
 * home's SELECTED level, so a writer that assigns the level before adding
 * silently collapses every floor onto one -- and a verifier that only counted
 * walls and rooms would call that file perfect. The per-level tally at the end
 * is the check that catches it.
 *
 *   java -cp "SweetHome3D.jar;Furniture.jar;out" Sh3dVerify file.sh3d
 */
public class Sh3dVerify {
  public static void main(String[] args) throws Exception {
    if (args.length < 1) {
      System.err.println("usage: Sh3dVerify <file.sh3d>");
      System.exit(2);
    }

    Home home = new HomeFileRecorder().readHome(args[0]);

    System.out.println("OPENED OK: " + args[0]);
    System.out.println("  name        : " + home.getName());
    System.out.println("  wallHeight  : " + home.getWallHeight());
    System.out.println("  selected    : " + levelName(home.getSelectedLevel()));

    // Counts per level, so a collapse onto one floor is visible at a glance.
    Map<String, int[]> tally = new LinkedHashMap<>();

    System.out.println("  levels      : " + home.getLevels().size());
    for (Level level : home.getLevels()) {
      System.out.printf("      %-12s elevation=%.1f floorThickness=%.1f height=%.1f%n",
          level.getName(), level.getElevation(), level.getFloorThickness(), level.getHeight());
      tally.put(level.getName(), new int[3]);
    }

    System.out.println("  walls       : " + home.getWalls().size());
    for (Wall wall : home.getWalls()) {
      String lv = levelName(wall.getLevel());
      count(tally, lv, 0);
      System.out.printf(
          "      [%-10s] (%.0f,%.0f)->(%.0f,%.0f) thickness=%.0f height=%-6s tex=%s%n",
          lv, wall.getXStart(), wall.getYStart(), wall.getXEnd(), wall.getYEnd(),
          wall.getThickness(), wall.getHeight(),
          sides(wall.getLeftSideTexture(), wall.getRightSideTexture()));
    }

    System.out.println("  rooms       : " + home.getRooms().size());
    for (Room room : home.getRooms()) {
      String lv = levelName(room.getLevel());
      count(tally, lv, 1);
      System.out.printf("      [%-10s] %-14s points=%d area=%7.2f m2 tex=%s%n",
          lv, room.getName(), room.getPoints().length, room.getArea() / 10000f,
          sides(room.getFloorTexture(), room.getCeilingTexture()));
    }

    // The plugin matches furniture by NAME, so that is what matters most here.
    int lights = 0;
    for (HomePieceOfFurniture piece : home.getFurniture()) {
      if (piece instanceof HomeLight) {
        lights++;
        HomeLight light = (HomeLight) piece;
        String lv = levelName(light.getLevel());
        count(tally, lv, 2);
        System.out.printf(
            "  LIGHT  [%-10s] name=%-45s x=%.0f y=%.0f elev=%.0f power=%.2f sources=%d%n",
            lv, light.getName(), light.getX(), light.getY(), light.getElevation(),
            light.getPower(), light.getLightSources().length);
      }
    }
    System.out.println("  lights      : " + lights);

    System.out.println("  per level   :");
    for (Map.Entry<String, int[]> e : tally.entrySet()) {
      int[] c = e.getValue();
      System.out.printf("      %-12s walls=%-4d rooms=%-4d lights=%-4d%n",
          e.getKey(), c[0], c[1], c[2]);
    }

    boolean empty = home.getLevels().isEmpty() || home.getWalls().isEmpty();
    // A level that ended up holding nothing at all is the signature of the
    // add-then-setLevel trap, not of a legitimately empty floor.
    boolean orphaned = false;
    for (Map.Entry<String, int[]> e : tally.entrySet()) {
      int[] c = e.getValue();
      if (c[0] == 0 && c[1] == 0 && c[2] == 0) {
        System.err.println("WARNING: level '" + e.getKey() + "' holds nothing");
        orphaned = true;
      }
    }
    if (empty) {
      System.err.println("WARNING: home opened but looks empty");
    }
    if (empty || orphaned) {
      System.exit(1);
    }
  }

  /**
   * Compact "which of the two surfaces carry a texture" marker.
   *
   * Walls report left/right, rooms floor/ceiling. Named rather than a bare
   * boolean because a texture landing on the wrong side of a wall looks exactly
   * like no texture at all from the side you happen to be rendering.
   */
  private static String sides(HomeTexture a, HomeTexture b) {
    if (a == null && b == null) {
      return "-";
    }
    return (a == null ? "-" : a.getName()) + "/" + (b == null ? "-" : b.getName());
  }

  /** Objects with no level report "(none)" rather than throwing. */
  private static String levelName(Level level) {
    return level == null ? "(none)" : level.getName();
  }

  private static void count(Map<String, int[]> tally, String levelName, int slot) {
    tally.computeIfAbsent(levelName, k -> new int[3])[slot]++;
  }
}
