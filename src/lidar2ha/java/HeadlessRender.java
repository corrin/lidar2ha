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

    // Flags anywhere, not just as args[1]. The old positional handling meant
    // `HeadlessRender model.sh3d out --list` silently rendered.
    boolean listOnly = false;
    String outDir = null;
    Integer width = null;
    Integer height = null;
    for (int i = 1; i < args.length; i++) {
      if ("--list".equals(args[i])) {
        listOnly = true;
      } else if (outDir == null) {
        outDir = args[i];
      } else if (width == null) {
        width = Integer.valueOf(args[i]);
      } else if (height == null) {
        height = Integer.valueOf(args[i]);
      }
    }
    // Both or neither: a lone width used to be accepted and then ignored, so a
    // render came out at the wrong size with nothing said about it.
    if ((width == null) != (height == null)) {
      System.err.println("give both width and height, or neither");
      System.exit(2);
    }

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

    // The mixing mode decides how many combinations get rendered, so it has to
    // be set before the count means anything -- including for --list.
    //
    // CSS renders one image per light and lets the browser add them together.
    // OVERLAY renders every combination of the lights IN EACH ROOM, and FULL
    // every combination in the house -- which is 2^n, so a house with twenty
    // lights is a million images. Defaulting to anything else would be a way to
    // spend a fortnight by accident.
    controller.setLightMixingMode(Controller.LightMixingMode.valueOf(
        System.getProperty("mixing", "CSS").toUpperCase()));
    System.out.println("total renders    : " + controller.getNumberOfTotalRenders());

    // Reported BEFORE rendering, and before --list returns, because this is the
    // number that decides whether to start at all. The plugin raytraces once
    // per light state, so a house with plenty of entities can mean hours; the
    // point of --list is to find that out for free.
    if (listOnly) {
      return;
    }

    if (outDir != null) {
      controller.setOutputDirectory(outDir);
    }
    if (width != null) {
      controller.setRenderWidth(width);
      controller.setRenderHeight(height);
    }
    controller.setRenderer(Controller.Renderer.valueOf(
        System.getProperty("renderer", "SUNFLOW").toUpperCase()));
    controller.setImageFormat(Controller.ImageFormat.valueOf(
        System.getProperty("imageFormat", "PNG").toUpperCase()));
    if (System.getProperty("sensitivity") != null) {
      controller.setSensitivity(Integer.parseInt(System.getProperty("sensitivity")));
    }
    // "Use existing renders": regenerate the floor plan and YAML from images
    // already on disk, without raytracing again.
    controller.setUserExistingRenders(
        Boolean.parseBoolean(System.getProperty("useExistingRenders", "false")));
    // Quality is not merely a speed dial. Sweet Home 3D's LOW settings capture
    // the Java3D OpenGL view instead of raytracing, so they need a working
    // offscreen GL context -- and when that is unavailable the render does not
    // fail, it silently returns a BLANK frame in about a second per image. The
    // high settings go through PhotoRenderer (SunFlow, or YafaRay when its
    // natives are on the library path), which is software and works anywhere.
    //
    // Default to HIGH: a slow correct render beats a fast blank one. Override
    // with -Dquality=LOW when a GL context is known good and speed matters.
    String quality = System.getProperty("quality", "HIGH").toUpperCase();
    controller.setQuality(Controller.Quality.valueOf(quality));

    System.out.println("quality          : " + quality);
    System.out.println("renderer         : " + controller.getRenderer());
    System.out.println("mixing           : " + controller.getLightMixingMode());
    System.out.println("reusing renders  : " + controller.getUserExistingRenders());
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
