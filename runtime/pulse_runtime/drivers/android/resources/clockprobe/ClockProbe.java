import java.io.BufferedReader;
import java.io.InputStreamReader;

/**
 * Pulse clock probe (runs on the device via app_process).
 * For every stdin line, prints System.nanoTime() / 1000 (CLOCK_MONOTONIC, microseconds),
 * the same time base as MediaCodec surface-input presentationTimeUs used by scrcpy PTS.
 */
public final class ClockProbe {
    public static void main(String[] args) throws Exception {
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
        System.out.println("ready " + (System.nanoTime() / 1000));
        System.out.flush();
        String line;
        while ((line = in.readLine()) != null) {
            if (line.equals("q")) break;
            System.out.println(line + " " + (System.nanoTime() / 1000));
            System.out.flush();
        }
    }
}
