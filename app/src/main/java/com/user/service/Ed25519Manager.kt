package com.user.service

import android.content.Context
import android.util.Base64
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.MessageDigest
import java.security.Signature
import java.security.spec.PKCS8EncodedKeySpec
import java.security.spec.X509EncodedKeySpec

class Ed25519Manager(context: Context) {

    private val prefs = context.getSharedPreferences("ed25519_prefs", Context.MODE_PRIVATE)
    private val keyPair: KeyPair by lazy { loadOrGenerate() }

    val publicKeyBase64url: String
        get() {
            val spki = keyPair.public.encoded
            val raw = spki.takeLast(32).toByteArray()
            return Base64.encodeToString(
                raw, Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING
            )
        }

    val deviceId: String
        get() {
            val spki = keyPair.public.encoded
            val raw = spki.takeLast(32).toByteArray()
            val digest = MessageDigest.getInstance("SHA-256").digest(raw)
            return digest.joinToString("") { "%02x".format(it) }
        }

    fun sign(payload: String): String {
        val sig = Signature.getInstance("Ed25519").apply {
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
            return try {
                // API 33+ has native Ed25519
                val f = KeyFactory.getInstance("Ed25519")
                val priv = f.generatePrivate(
                    PKCS8EncodedKeySpec(Base64.decode(savedPriv, Base64.DEFAULT))
                )
                val pub = f.generatePublic(
                    X509EncodedKeySpec(Base64.decode(savedPub, Base64.DEFAULT))
                )
                KeyPair(pub, priv)
            } catch (e: Exception) {
                generateAndSave()
            }
        }
        return generateAndSave()
    }

    private fun generateAndSave(): KeyPair {
        // Try native Ed25519 (API 33+) first, fall back to BC
        val kp = try {
            KeyPairGenerator.getInstance("Ed25519").generateKeyPair()
        } catch (e: Exception) {
            generateWithBC()
        }
        prefs.edit()
            .putString("priv", Base64.encodeToString(kp.private.encoded, Base64.DEFAULT))
            .putString("pub",  Base64.encodeToString(kp.public.encoded,  Base64.DEFAULT))
            .apply()
        return kp
    }

    private fun generateWithBC(): KeyPair {
        // Manually register BC and generate
        val provider = org.bouncycastle.jce.provider.BouncyCastleProvider()
        java.security.Security.removeProvider("BC")
        java.security.Security.insertProviderAt(provider, 1)
        return KeyPairGenerator.getInstance("Ed25519", provider).generateKeyPair()
    }
}
