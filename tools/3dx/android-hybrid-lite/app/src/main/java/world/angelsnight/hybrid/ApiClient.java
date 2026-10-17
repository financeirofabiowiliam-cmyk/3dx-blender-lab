package world.angelsnight.hybrid;

import android.app.ActivityManager;
import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.BatteryManager;
import android.util.Base64;
import org.json.JSONArray;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** 3DX Hybrid transport with strict same-origin HTTPS and bounded references. */
public final class ApiClient {
    private static final String ORIGIN="https://threedx-studio-blender-qa.onrender.com";
    private final Context context;
    private final String token;
    public ApiClient(Context context,String token){this.context=context;this.token=token;}
    private static byte[] readLimited(InputStream input,int max) throws Exception {
        if(input==null)throw new IllegalStateException("Resposta vazia.");
        try(InputStream in=input;ByteArrayOutputStream out=new ByteArrayOutputStream()){
            byte[] buffer=new byte[32768];int n;
            while((n=in.read(buffer))!=-1){
                if(out.size()+n>max)throw new IllegalArgumentException("Resposta grande demais.");
                out.write(buffer,0,n);
            }
            return out.toByteArray();
        }
    }
    private JSONObject post(String route,JSONObject body,int maxResponse) throws Exception {
        if(!route.startsWith("/api/v1/worker/")||route.contains(".."))
            throw new IllegalArgumentException("Rota não autorizada.");
        HttpURLConnection connection=(HttpURLConnection)new URL(ORIGIN+route).openConnection();
        try{
            connection.setRequestMethod("POST");connection.setConnectTimeout(12000);
            connection.setReadTimeout(60000);connection.setUseCaches(false);
            connection.setDoOutput(true);
            connection.setRequestProperty("Authorization","Bearer "+token);
            connection.setRequestProperty("Content-Type","application/json");
            connection.setRequestProperty("X-3DX-Asset-Contract","3dx-asset-package/1");
            byte[] payload=body.toString().getBytes(StandardCharsets.UTF_8);
            if(payload.length>40*1024*1024)
                throw new IllegalArgumentException("Tarefa acima do limite.");
            connection.setFixedLengthStreamingMode(payload.length);
            try(OutputStream out=connection.getOutputStream()){out.write(payload);}
            int code=connection.getResponseCode();
            if(code==401||code==403)throw new SecurityException("Chave do 3DX não autorizada.");
            InputStream input=code<400?connection.getInputStream():connection.getErrorStream();
            String value=new String(readLimited(input,maxResponse),StandardCharsets.UTF_8);
            if(code>=400)throw new IllegalStateException("Servidor HTTP "+code);
            return new JSONObject(value);
        }finally{connection.disconnect();}
    }
    public JSONObject claim() throws Exception {
        return post("/api/v1/worker/mobile/claim",new JSONObject(),40*1024*1024).optJSONObject("task");
    }
    public void abandon(String job,String lease){
        try{post("/api/v1/worker/abandon",
                new JSONObject().put("job_id",job).put("lease",lease),1024*1024);
        }catch(Exception ignored){} // Lease timeout on Render supplies an additional fallback.
    }
    public void complete(JSONObject job,JSONArray refs) throws Exception {
        post("/api/v1/worker/mobile/complete",
                new JSONObject().put("job_id",job.getString("job_id"))
                .put("lease",job.getString("lease")).put("references",refs),2*1024*1024);
    }
    public void checkMemoryAndBattery(){
        ActivityManager.MemoryInfo memory=new ActivityManager.MemoryInfo();
        ((ActivityManager)context.getSystemService(Context.ACTIVITY_SERVICE)).getMemoryInfo(memory);
        if(memory.lowMemory||memory.availMem<400L*1024*1024)
            throw new IllegalStateException("Menos de 400 MB de RAM livres.");
        BatteryManager bm=(BatteryManager)context.getSystemService(Context.BATTERY_SERVICE);
        int level=bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
        if(level>=0&&level<20&&!isCharging())throw new IllegalStateException("Bateria abaixo de 20%.");
    }
    private boolean isCharging(){
        android.content.Intent result=context.registerReceiver(null,
                new android.content.IntentFilter(android.content.Intent.ACTION_BATTERY_CHANGED));
        if(result==null)return false;
        int status=result.getIntExtra(BatteryManager.EXTRA_STATUS,-1);
        return status==BatteryManager.BATTERY_STATUS_CHARGING
                ||status==BatteryManager.BATTERY_STATUS_FULL;
    }
    public JSONArray prepare(JSONObject task) throws Exception {
        if(!"3dx-local-preprocess/1".equals(task.optString("contract"))
                ||!"android".equals(task.optString("device")))
            throw new IllegalArgumentException("Contrato do 3DX incompatível.");
        JSONArray source=task.getJSONArray("references");
        if(source.length()<1||source.length()>8)
            throw new IllegalArgumentException("Quantidade de fotos inválida.");
        JSONArray output=new JSONArray();
        for(int i=0;i<source.length();i++){
            checkMemoryAndBattery();
            JSONObject ref=source.getJSONObject(i);
            String view=ref.getString("view");
            if(!(view.equals("front")||view.equals("back")||view.equals("left")
                    ||view.equals("right")||view.equals("additional")))
                throw new IllegalArgumentException("Vista inválida.");
            String encoded=ref.getString("data");
            if(encoded.length()>5592416)throw new IllegalArgumentException("Foto acima de 4 MiB.");
            byte[] data=Base64.decode(encoded,Base64.DEFAULT);
            if(data.length>4*1024*1024)throw new IllegalArgumentException("Foto muito grande.");
            BitmapFactory.Options opt=new BitmapFactory.Options();
            opt.inJustDecodeBounds=true;
            BitmapFactory.decodeByteArray(data,0,data.length,opt);
            if(opt.outWidth<32||opt.outHeight<32||Math.max(opt.outWidth,opt.outHeight)>1024)
                throw new IllegalArgumentException("Resolução incompatível.");
            int sample=1;
            while(Math.max(opt.outWidth/sample,opt.outHeight/sample)>768)sample*=2;
            opt.inJustDecodeBounds=false;opt.inSampleSize=sample;
            opt.inPreferredConfig=Bitmap.Config.ARGB_8888;
            Bitmap bitmap=BitmapFactory.decodeByteArray(data,0,data.length,opt);
            data=null;
            if(bitmap==null)throw new IllegalArgumentException("Foto não decodificável.");
            Bitmap scaled=bitmap;
            try{
                double scale=Math.min(1.0,768.0/Math.max(bitmap.getWidth(),bitmap.getHeight()));
                if(scale<1)scaled=Bitmap.createScaledBitmap(bitmap,
                        Math.max(32,(int)Math.round(bitmap.getWidth()*scale)),
                        Math.max(32,(int)Math.round(bitmap.getHeight()*scale)),true);
                ByteArrayOutputStream png=new ByteArrayOutputStream();
                if(!scaled.compress(Bitmap.CompressFormat.PNG,100,png))
                    throw new IllegalStateException("Falha ao preparar PNG.");
                byte[] packed=png.toByteArray();
                if(packed.length>4*1024*1024)
                    throw new IllegalArgumentException("Foto ainda acima de 4 MiB.");
                output.put(new JSONObject().put("view",view)
                        .put("data",Base64.encodeToString(packed,Base64.NO_WRAP)));
            }finally{
                if(scaled!=bitmap)scaled.recycle();
                bitmap.recycle();
            }
        }
        return output;
    }
}
