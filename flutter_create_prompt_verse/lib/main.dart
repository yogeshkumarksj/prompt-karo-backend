import 'package:flutter/material.dart';
import 'core/app_theme.dart'; // अगर यहाँ लाल लाइन आये तो घबराएं नहीं
import 'screens/main_screen.dart'; // अगर यहाँ लाल लाइन आये तो घबराएं नहीं

void main() {
  runApp(const PromptVerse());
}

class PromptVerse extends StatelessWidget {
  const PromptVerse({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'PromptVerse',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.darkTheme, // हमारा बनाया हुआ थीम
      home: const MainScreen(), // हमारी बनाई हुई पहली स्क्रीन
    );
  }
}