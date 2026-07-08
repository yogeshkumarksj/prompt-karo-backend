import 'package:flutter/material.dart';
import 'home_screen.dart'; // 1. यह नया इम्पोर्ट जोड़ा गया है

class MainScreen extends StatefulWidget {
  const MainScreen({super.key});

  @override
  State<MainScreen> createState() => _MainScreenState();
}

class _MainScreenState extends State<MainScreen> {
  int _currentIndex = 0;

  // 2. यहाँ हमने पहले 'Center' टेक्स्ट को हटाकर 'HomeScreen()' डाल दिया है
  final List<Widget> _pages = [
    HomeScreen(), // अब पहले बटन पर क्लिक करने पर ग्रिड दिखेगा
    const Center(key: ValueKey(1), child: Text('🔍 Search AI Art', style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold))),
    const Center(key: ValueKey(2), child: Text('💾 Saved Library', style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold))),
  ];

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: AnimatedSwitcher(
        duration: const Duration(milliseconds: 300),
        // यहाँ हमने 'child' को थोड़ा बदला है ताकि एनीमेशन सही रहे
        child: _pages[_currentIndex],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _currentIndex,
        onDestinationSelected: (index) {
          setState(() {
            _currentIndex = index;
          });
        },
        backgroundColor: const Color(0xFF18181B),
        indicatorColor: const Color(0xFF8B5CF6).withOpacity(0.2),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.auto_awesome_outlined), 
            selectedIcon: Icon(Icons.auto_awesome, color: Color(0xFF8B5CF6)),
            label: 'Explore'
          ),
          NavigationDestination(
            icon: Icon(Icons.search_rounded), 
            selectedIcon: Icon(Icons.search, color: Color(0xFF8B5CF6)),
            label: 'Search'
          ),
          NavigationDestination(
            icon: Icon(Icons.bookmarks_outlined), 
            selectedIcon: Icon(Icons.bookmarks, color: Color(0xFF8B5CF6)),
            label: 'Saved'
          ),
        ],
      ),
    );
  }
}