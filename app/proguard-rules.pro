# See https://developer.android.com/studio/build/gradle-tips#configure-proguard-optimizations-in-buildtoml for more information
# Remove flags used by Crashlytics to reduce size
-optimizations false

# Include fields that the caller will not use
-keepclassmembernames,allowobfuscation
class * {
    volatile <fields>;
}
