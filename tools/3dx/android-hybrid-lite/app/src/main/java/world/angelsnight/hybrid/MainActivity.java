package world.angelsnight.hybrid;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.text.InputType;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/** Simple Galaxy A11 control panel; native worker needs no WebView or open tab. */
public final class MainActivity extends Activity {
    private EditText token;
    private TextView status;
    private CheckBox wifi,boot;
    private int px(int d){return (int)(d*getResources().getDisplayMetrics().density+.5f);}
    private TextView label(LinearLayout root,String text,int size){
        TextView result=new TextView(this);
        result.setText(text);result.setTextColor(Color.rgb(215,226,244));
        result.setTextSize(size);result.setPadding(0,px(8),0,px(8));
        root.addView(result);return result;
    }
    private void button(LinearLayout root,String caption,Runnable action){
        Button b=new Button(this);
        b.setText(caption);b.setAllCaps(false);b.setTextSize(15);
        root.addView(b,new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,px(56)));
        b.setOnClickListener(v->action.run());
    }
    @Override public void onCreate(Bundle saved){
        super.onCreate(saved);
        getWindow().setStatusBarColor(Color.rgb(13,20,34));
        ScrollView scroll=new ScrollView(this);scroll.setFillViewport(true);
        LinearLayout root=new LinearLayout(this);root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(px(20),px(25),px(20),px(25));
        root.setBackgroundColor(Color.rgb(13,20,34));
        scroll.addView(root);setContentView(scroll);
        label(root,"3DX HYBRID MOBILE",24);
        label(root,"Galaxy A11 • auxílio leve de imagens • Blender permanece no Render",14);
        label(root,"Chave privada do mesmo laboratório 3DX",14);
        token=new EditText(this);
        token.setSingleLine(true);token.setTextSize(15);token.setTextColor(Color.WHITE);
        token.setHintTextColor(Color.LTGRAY);token.setHint("Cole a chave de acesso");
        token.setInputType(InputType.TYPE_CLASS_TEXT|InputType.TYPE_TEXT_VARIATION_PASSWORD);
        root.addView(token);
        wifi=new CheckBox(this);wifi.setText("Usar somente Wi-Fi (recomendado)");
        wifi.setTextColor(Color.WHITE);
        wifi.setChecked(SecureToken.preferences(this).getBoolean("wifi",true));
        root.addView(wifi);
        boot=new CheckBox(this);boot.setText("Tentar reconectar após reiniciar o aparelho");
        boot.setTextColor(Color.WHITE);
        boot.setChecked(SecureToken.preferences(this).getBoolean("boot",false));
        root.addView(boot);
        button(root,"Conectar e manter auxiliar ativo",()->{
            try{
                String value=token.getText().toString().trim();
                if(!value.isEmpty())SecureToken.save(this,value);
                else if(SecureToken.read(this)==null)
                    throw new IllegalStateException("Informe uma chave de acesso.");
                SecureToken.preferences(this).edit().putBoolean("wifi",wifi.isChecked())
                        .putBoolean("boot",boot.isChecked()).putBoolean("enabled",true).apply();
                token.setText("");
                if(Build.VERSION.SDK_INT>=33 &&
                        checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED)
                    requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},3);
                startForegroundService(new Intent(this,HybridService.class)
                        .setAction(HybridService.ACTION_START));
                status.setText("Iniciando conexão HTTPS. Verifique a notificação.");
            }catch(Exception error){
                status.setText("Não foi possível ativar: "+error.getMessage());
            }
        });
        button(root,"Desconectar do laboratório",()->{
            startService(new Intent(this,HybridService.class)
                    .setAction(HybridService.ACTION_STOP));
            SecureToken.preferences(this).edit().putBoolean("enabled",false).apply();
            status.setText("Desconectado pelo usuário.");
        });
        button(root,"Apagar chave privada salva",()->{
            startService(new Intent(this,HybridService.class)
                    .setAction(HybridService.ACTION_STOP));
            SecureToken.clear(this);
            status.setText("Chave apagada. Auxiliar desconectado.");
        });
        button(root,"Abrir ajustes de bateria do aplicativo",()->{
            Intent settings=new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.parse("package:"+getPackageName()));
            startActivity(settings);
        });
        status=label(root,"Estado: "+
                SecureToken.preferences(this).getString("status","parado"),14);
        label(root,"A Samsung pode interromper serviços por bateria, rede ou aquecimento. "
                +"Nas configurações do aplicativo, escolha bateria sem restrições, quando disponível. "
                +"Use Wi-Fi, mantenha o celular ventilado e evite bateria abaixo de 20%. "
                +"No PC, marque Android como auxiliar do 3DX Studio. Não soma RAM ao Render.",13);
    }
    @Override protected void onResume(){
        super.onResume();
        if(status!=null)status.setText("Estado: "+
                SecureToken.preferences(this).getString("status","parado"));
    }
}
