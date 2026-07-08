import 'package:flutter/material.dart';
import 'package:cached_network_image/cached_network_image.dart';
import '../models/prompt_model.dart';
import '../screens/prompt_detail_screen.dart'; // 1. डिटेल स्क्रीन को इम्पोर्ट किया

class PromptCard extends StatelessWidget {
  final Prompt prompt;
  const PromptCard({super.key, required this.prompt});

  @override
  Widget build(BuildContext context) {
    // 2. पूरे कार्ड को InkWell से घेरा ताकि क्लिक हो सके
    return InkWell(
      onTap: () {
        // 3. क्लिक करने पर डिटेल पेज पर जाने का लॉजिक
        Navigator.push(
          context,
          MaterialPageRoute(
            builder: (context) => PromptDetailScreen(prompt: prompt),
          ),
        );
      },
      child: Card(
        clipBehavior: Clip.antiAlias,
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // इमेज सेक्शन
            CachedNetworkImage(
              imageUrl: prompt.imageUrl,
              placeholder: (context, url) => Container(
                height: 150, 
                color: Colors.grey[900],
                child: const Center(child: CircularProgressIndicator(strokeWidth: 2)),
              ),
              errorWidget: (context, url, error) => const SizedBox(
                height: 150,
                child: Center(child: Icon(Icons.broken_image, color: Colors.grey)),
              ),
              fit: BoxFit.cover,
            ),
            // टाइटल और कैटेगरी
            Padding(
              padding: const EdgeInsets.all(10),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    prompt.title, 
                    style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 14),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                  ),
                  const SizedBox(height: 4),
                  Text(
                    "#${prompt.category}", 
                    style: const TextStyle(color: Color(0xFF8B5CF6), fontSize: 12),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}