package world.angelsnight.hybrid;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/** Optional boot resume, subject to Android and Samsung background restrictions. */
public final class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context,Intent intent) {
        if(!Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction()))return;
        if(Build.VERSION.SDK_INT>=35)return;
        if(!SecureToken.preferences(context).getBoolean("enabled",false)
                ||!SecureToken.preferences(context).getBoolean("boot",false))return;
        try {
            context.startForegroundService(
                    new Intent(context,HybridService.class).setAction(HybridService.ACTION_START));
        }catch(Exception ignored) {
            // Android or the device manufacturer may refuse a background start.
        }
    }
}
