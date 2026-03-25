package com.user.service

import android.content.Context
import android.util.Base64
import android.util.Log
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.Security
import java.security.Signature
import java.security.spec.PKCS8EncodedKeySpec
import java.security.spec.X509EncodedKeySpec
import org.bouncycastle.jce.provider.BouncyCastleProvider

/**
 * Manages Ed25519 keys for device identification and gateway authentication.
 * Uses BouncyCastle to ensure consistent behavior across different Android versions
 * and to avoid issues with the Android KeyStore provider.
 */
class Ed25519Manager(context: Context) {

    private val prefs = context.getSharedPreferences("ed25519_prefs", Context.MODE_PRIVATE)
    private lateinit var keyPair: KeyPair

    companion object {
        private const val TAG = "Ed25519Manager"
        private val bcProvider = BouncyCastleProvider()

        init {
            // Ensure BouncyCastle is registered and preferred for our operations
            Security.removeProvider("BC")
            Security.addProvider(bcProvider)
        }
    }

    init {
        try {
            keyPair = loadOrGenerate()
        } catch (e: Exception) {
            Log.e(TAG, "Critical failure in key initialization", e)
            // Fallback to a standard EC key if Ed25519 is completely unavailable,
            // though this will likely cause authentication failures with the gateway.
            val kpg = KeyPairGenerator.getInstance("EC")
            kpg.initialize(256)
            keyPair = kpg.generateKeyPair()
        }
    }

    val publicKeyBase64url: String
        get() {
            val spki = keyPair.public.encoded
            // Ed25519 raw public key is the last 32 bytes of the SPKI encoding.
            val raw = if (spki.size >= 32) spki.takeLast(32).toByteArray() else spki
            return Base64.encodeToString(
                raw, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING
            )
        }

    val deviceId: String
        get() {
            val spki = keyPair.public.encoded
            val raw = if (spki.size >= 32) spki.takeLast(32).toByteArray() else spki
            val digest = MessageDigest.getInstance("SHA-256").digest(raw)
            return digest.joinToString("") { "%02x".format(it) }
        }

    fun persistDeviceId() {
        prefs.edit().putString("device_id_paired", deviceId).apply()
    }

    fun isPaired(): Boolean {
        return prefs.getString("device_id_paired", null) != null
    }

    fun sign(payload: String): String {
        val sig = try {
            // Explicitly use BouncyCastle for signing
            Signature.getInstance("Ed25519", bcProvider)
        } catch (e: Exception) {
            try {
                Signature.getInstance("EdDSA", bcProvider)
            } catch (e2: Exception) {
                Signature.getInstance("Ed25519")
            }
        }.apply {
            initSign(keyPair.private)
            update(payload.toByteArray(Charsets.UTF_8))
        }
        return Base64.encodeToString(
            sig.sign(),
            Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING
        )
    }

    fun buildSignature(
        signedAt: Long,
        credential: String,
        nonce: String,
        scopes: String = "operator.admin,operator.approvals,operator.pairing,operator.read,operator.write"
    ): String {
        val payload = listOf(
            "v2", deviceId, "gateway-client", "backend",
            "operator", scopes, signedAt.toString(), credential, nonce
        ).joinToString("|")
        return sign(payload)
    }

    private fun loadOrGenerate(): KeyPair {
        val savedPriv = prefs.getString("priv", null)
        val savedPub  = prefs.getString("pub",  null)
        if (savedPriv != null && savedPub != null) {
            val privBytes = Base64.decode(savedPriv, Base64.DEFAULT)
            val pubBytes  = Base64.decode(savedPub,  Base64.DEFAULT)

            return try {
                // Try to load using BouncyCastle specifically
                val f = KeyFactory.getInstance("Ed25519", bcProvider)
                KeyPair(
                    f.generatePublic(X509EncodedKeySpec(pubBytes)),
                    f.generatePrivate(PKCS8EncodedKeySpec(privBytes))
                )
            } catch (e: Exception) {
                Log.w(TAG, "Saved keys invalid or incompatible, generating new ones")
                generateAndSave()
            }
        }
        return generateAndSave()
    }

    private fun generateAndSave(): KeyPair {
        val kp = try {
            // Try generating with BC using both "Ed25519" and "EdDSA" names
            try {
                KeyPairGenerator.getInstance("Ed25519", bcProvider).generateKeyPair()
            } catch (e: Exception) {
                KeyPairGenerator.getInstance("EdDSA", bcProvider).generateKeyPair()
            }
        } catch (e: Exception) {
            Log.e(TAG, "BC Ed25519 keygen failed: ${e.message}")
            // DO NOT fall back to system provider's Ed25519 without provider instance
            // as it often returns a KeyStore generator that requires complex initialization.
            throw e
        }

        // Save to prefs for persistence
        prefs.edit()
            .putString("priv", Base64.encodeToString(kp.private.encoded, Base64.DEFAULT))
            .putString("pub",  Base64.encodeToString(kp.public.encoded,  Base64.DEFAULT))
            .apply()

        return kp
    }
}
