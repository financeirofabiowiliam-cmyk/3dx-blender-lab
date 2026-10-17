package world.angelsnight.hybrid;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;
import java.nio.charset.StandardCharsets;
import java.security.KeyStore;
import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/** Protects the 3DX access key with Android Keystore AES-GCM. */
public final class SecureToken {
    private static final String ALIAS="3dx-hybrid-mobile-token-v1";
    private static final String STORAGE="3dx-hybrid-settings";
    private SecureToken() {}
    public static SharedPreferences preferences(Context context) {
        return context.getSharedPreferences(STORAGE,Context.MODE_PRIVATE);
    }
    private static SecretKey key() throws Exception {
        KeyStore store=KeyStore.getInstance("AndroidKeyStore");
        store.load(null);
        KeyStore.Entry existing=store.getEntry(ALIAS,null);
        if(existing instanceof KeyStore.SecretKeyEntry)
            return ((KeyStore.SecretKeyEntry) existing).getSecretKey();
        KeyGenerator generator=KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES,"AndroidKeyStore");
        generator.init(new KeyGenParameterSpec.Builder(ALIAS,
                KeyProperties.PURPOSE_ENCRYPT|KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256).build());
        return generator.generateKey();
    }
    public static void save(Context context,String token) throws Exception {
        if(token==null||token.trim().length()<8||token.length()>4096||token.indexOf('\n')>=0)
            throw new IllegalArgumentException("Chave de acesso inválida.");
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE,key());
        byte[] encrypted=cipher.doFinal(token.trim().getBytes(StandardCharsets.UTF_8));
        preferences(context).edit()
                .putString("iv",Base64.encodeToString(cipher.getIV(),Base64.NO_WRAP))
                .putString("secret",Base64.encodeToString(encrypted,Base64.NO_WRAP)).apply();
    }
    public static String read(Context context) throws Exception {
        SharedPreferences pref=preferences(context);
        String secret=pref.getString("secret",null),iv=pref.getString("iv",null);
        if(secret==null||iv==null)return null;
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.DECRYPT_MODE,key(),
                new GCMParameterSpec(128,Base64.decode(iv,Base64.NO_WRAP)));
        return new String(cipher.doFinal(Base64.decode(secret,Base64.NO_WRAP)),StandardCharsets.UTF_8);
    }
    public static void clear(Context context) {
        preferences(context).edit().remove("iv").remove("secret").putBoolean("enabled",false).apply();
    }
}
