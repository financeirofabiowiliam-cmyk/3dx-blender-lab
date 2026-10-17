package world.angelsnight.hybrid;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;
import org.json.JSONArray;
import org.json.JSONObject;
import java.util.concurrent.atomic.AtomicBoolean;

/** Visible user-controlled dataSync foreground service, with Render fallback. */
public final class HybridService extends Service {
    public static final String ACTION_START="world.angelsnight.hybrid.START";
    public static final String ACTION_STOP="world.angelsnight.hybrid.STOP";
    private static final String CHANNEL="3dx_hybrid_status";
    private static final int ID=303;
    private final AtomicBoolean live=new AtomicBoolean(false);
    private volatile Thread worker;

    @Override public IBinder onBind(Intent intent){return null;}
    @Override public void onCreate(){
        super.onCreate();
        NotificationChannel channel=new NotificationChannel(
                CHANNEL,"3DX auxiliar ativo",NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("Tarefas de imagens do 3DX em segundo plano");
        ((NotificationManager)getSystemService(NOTIFICATION_SERVICE)).createNotificationChannel(channel);
    }
    private Notification notification(String message){
        PendingIntent open=PendingIntent.getActivity(this,0,
                new Intent(this,MainActivity.class),
                PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop=PendingIntent.getService(this,1,
                new Intent(this,HybridService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_UPDATE_CURRENT|PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this,CHANNEL)
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle("3DX Hybrid Mobile ativo")
                .setContentText(message).setContentIntent(open).setOngoing(true)
                .addAction(new Notification.Action.Builder(
                        android.R.drawable.ic_media_pause,"Parar 3DX",stop).build())
                .build();
    }
    private void status(String text){
        SecureToken.preferences(this).edit().putString("status",text).apply();
        ((NotificationManager)getSystemService(NOTIFICATION_SERVICE)).notify(ID,notification(text));
    }
    private boolean wifiConnected(){
        ConnectivityManager cm=(ConnectivityManager)getSystemService(Context.CONNECTIVITY_SERVICE);
        Network network=cm.getActiveNetwork();
        NetworkCapabilities caps=network==null?null:cm.getNetworkCapabilities(network);
        return caps!=null&&caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                &&caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI);
    }
    @Override public int onStartCommand(Intent intent,int flags,int startId){
        String action=intent==null?ACTION_START:intent.getAction();
        if(ACTION_STOP.equals(action)){
            SecureToken.preferences(this).edit().putBoolean("enabled",false).apply();
            live.set(false);
            Thread old=worker;if(old!=null)old.interrupt();
            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();return START_NOT_STICKY;
        }
        if(!SecureToken.preferences(this).getBoolean("enabled",false)){
            stopSelf();return START_NOT_STICKY;
        }
        if(Build.VERSION.SDK_INT>=29)
            startForeground(ID,notification("Aguardando comunicação segura"),
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        else startForeground(ID,notification("Aguardando comunicação segura"));
        if(live.compareAndSet(false,true)){
            worker=new Thread(this::work,"3dx-mobile-worker");
            worker.start();
        }
        return START_STICKY;
    }
    private void work(){
        try{
            while(live.get()){
                try{
                    String key=SecureToken.read(this);
                    if(key==null){status("Chave ausente: conecte novamente.");break;}
                    if(SecureToken.preferences(this).getBoolean("wifi",true)&&!wifiConnected()){
                        status("Aguardando Wi-Fi para poupar dados móveis");
                        Thread.sleep(30000);continue;
                    }
                    PowerManager pm=(PowerManager)getSystemService(POWER_SERVICE);
                    PowerManager.WakeLock lock=pm.newWakeLock(
                            PowerManager.PARTIAL_WAKE_LOCK,"3dx:mobile-task");
                    JSONObject task=null;
                    ApiClient client=new ApiClient(this,key);
                    try{
                        lock.acquire(120000);
                        client.checkMemoryAndBattery();
                        task=client.claim();
                        if(task==null)status("Conectado; esperando tarefas do computador");
                        else{
                            status("Preparando fotografias do 3DX");
                            JSONArray prepared=client.prepare(task);
                            client.complete(task,prepared);
                            status("Concluído: Blender continuará no Render");
                        }
                    }catch(SecurityException denied){
                        status("Chave recusada; conecte novamente.");
                        SecureToken.preferences(this).edit().putBoolean("enabled",false).apply();
                        break;
                    }catch(Exception error){
                        if(task!=null)client.abandon(
                                task.optString("job_id"),task.optString("lease"));
                        status("Reconectando; falhas retornam à fila do Render");
                    }finally{
                        if(lock.isHeld())lock.release();
                    }
                    Thread.sleep(task==null?24000:5000);
                }catch(InterruptedException end){
                    Thread.currentThread().interrupt();break;
                }catch(Exception failure){
                    status("Aguardando rede, RAM e bateria disponíveis");
                    Thread.sleep(30000);
                }
            }
        }catch(InterruptedException end){
            Thread.currentThread().interrupt();
        }finally{
            live.set(false);
            stopSelf();
        }
    }
    @Override public void onDestroy(){
        live.set(false);
        Thread old=worker;if(old!=null)old.interrupt();
        super.onDestroy();
    }
}
