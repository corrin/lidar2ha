import com.eteks.sweethome3d.io.HomeFileRecorder;
import com.eteks.sweethome3d.model.Home;
import com.eteks.sweethome3d.model.HomeLight;
import com.eteks.sweethome3d.model.HomePieceOfFurniture;
import com.eteks.sweethome3d.model.Level;
import com.eteks.sweethome3d.model.Room;
import com.eteks.sweethome3d.model.Wall;

/**
 * Read a .sh3d back through HomeFileRecorder and report what is inside.
 *
 * This is the same code path Sweet Home 3D uses to open a file, so a clean run
 * here means the archive is loadable -- as opposed to merely *looking* like a
 * valid archive. Writing a file that structurally resembled a real one but
 * could not be opened is exactly the trap this exists to catch.
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

    System.out.println("  levels      : " + home.getLevels().size());
    for (Level level : home.getLevels()) {
      System.out.printf("      %-12s elevation=%.1f floorThickness=%.1f height=%.1f%n",
          level.getName(), level.getElevation(), level.getFloorThickness(), level.getHeight());
    }

    System.out.println("  walls       : " + home.getWalls().size());
    for (Wall wall : home.getWalls()) {
      System.out.printf("      (%.0f,%.0f)->(%.0f,%.0f) thickness=%.0f height=%s%n",
          wall.getXStart(), wall.getYStart(), wall.getXEnd(), wall.getYEnd(),
          wall.getThickness(), wall.getHeight());
    }

    System.out.println("  rooms       : " + home.getRooms().size());
    for (Room room : home.getRooms()) {
      System.out.printf("      %-14s points=%d area=%.2f m2%n",
          room.getName(), room.getPoints().length, room.getArea() / 10000f);
    }

    // The plugin matches furniture by NAME, so that is what matters most here.
    int lights = 0;
    for (HomePieceOfFurniture piece : home.getFurniture()) {
      if (piece instanceof HomeLight) {
        lights++;
        HomeLight light = (HomeLight) piece;
        System.out.printf("  LIGHT  name=%-45s x=%.0f y=%.0f elev=%.0f power=%.2f sources=%d%n",
            light.getName(), light.getX(), light.getY(), light.getElevation(),
            light.getPower(), light.getLightSources().length);
      }
    }
    System.out.println("  lights      : " + lights);

    if (home.getLevels().isEmpty() || home.getWalls().isEmpty()) {
      System.err.println("WARNING: home opened but looks empty");
      System.exit(1);
    }
  }
}
