import com.eteks.sweethome3d.io.DefaultUserPreferences;
import com.eteks.sweethome3d.io.HomeFileRecorder;
import com.eteks.sweethome3d.model.Home;
import com.eteks.sweethome3d.model.UserPreferences;
import com.shmuelzon.HomeAssistantFloorPlan.Controller;
import com.shmuelzon.HomeAssistantFloorPlan.Entity;

import java.util.List;

/**
 * Drive the home-assistant-floor-plan plugin without its GUI.
 *
 * The plugin separates Controller (logic) from Panel (Swing), and Controller's
 * constructor takes only a Home. So the whole render -- light detection,
 * per-light raytracing, floorplan/ output and floorplan.yaml -- can be run
 * from the command line.
 *
 *   java -cp "<sh3d jars>;<plugin .sh3p>;out" HeadlessRender model.sh3d outdir [width] [height]
 *
 * Pass --list to only report detected entities and exit without rendering.
 * That is the cheap check: if a light does not appear here, nothing downstream
 * matters, and it costs no render time to find out.
 */
public class HeadlessRender {
  public static void main(String[] args) throws Exception {
    // Sweet Home 3D bundles a 32-bit runtime and ships only javaw.exe, which
    // has no console. Java3D and YafaRay are 32-bit natives, so they must be
    // driven by that JVM -- which means logging to a file instead of stdout.
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

    if (args.length < 1) {
      System.err.println("usage: HeadlessRender <model.sh3d> [outdir] [width] [height]");
      System.err.println("       HeadlessRender <model.sh3d> --list");
      System.exit(2);
    }

    boolean listOnly = args.length > 1 && "--list".equals(args[1]);

    UserPreferences prefs = new DefaultUserPreferences();
    Home home = new HomeFileRecorder(9, false, prefs, false, true, true).readHome(args[0]);
    System.out.println("loaded " + args[0]
        + "  levels=" + home.getLevels().size()
        + " walls=" + home.getWalls().size()
        + " rooms=" + home.getRooms().size()
        + " furniture=" + home.getFurniture().size());

    Controller controller = new Controller(home);
    controller.loadDefaultSettings();

    if (controller.isProjectEmpty()) {
      System.err.println("ERROR: plugin reports the project is empty");
      System.exit(1);
    }

    // The detection check. The plugin matches furniture by NAME against
    // Home Assistant entity ids -- if a light is missing here it is almost
    // always because it is invisible or its power is 0.
    List<Entity> lights = controller.getLightEntities();
    System.out.println("detected light entities: " + lights.size());
    for (Entity e : lights) {
      System.out.println("    " + e.getName());
    }
    List<Entity> others = controller.getOtherEntities();
    System.out.println("detected other entities: " + others.size());
    for (Entity e : others) {
      System.out.println("    " + e.getName());
    }
    System.out.println("light groups (rooms): " + controller.getLightsGroups().keySet());

    if (listOnly) {
      return;
    }

    if (args.length > 1) {
      controller.setOutputDirectory(args[1]);
    }
    if (args.length > 3) {
      controller.setRenderWidth(Integer.parseInt(args[2]));
      controller.setRenderHeight(Integer.parseInt(args[3]));
    }
    controller.setQuality(Controller.Quality.LOW);
    controller.setLightMixingMode(Controller.LightMixingMode.CSS);

    System.out.println("output directory : " + controller.getOutputDirectory());
    System.out.println("render size      : "
        + controller.getRenderWidth() + "x" + controller.getRenderHeight());
    System.out.println("total renders    : " + controller.getNumberOfTotalRenders());

    long start = System.currentTimeMillis();
    controller.render();
    long ms = System.currentTimeMillis() - start;
    System.out.printf("DONE in %.1f s (%d renders)%n",
        ms / 1000.0, controller.getNumberOfTotalRenders());
  }
}
