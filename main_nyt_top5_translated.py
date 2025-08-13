import os
import json
import smtplib
import argparse
import locale
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import time

import requests
from dotenv import load_dotenv
from google.cloud import translate_v2 as translate

def load_configuration():
    """Loads settings from config.json and environment variables from .env."""
    load_dotenv()
    with open('config.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    required_vars = ['NYT_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS', 'EMAIL_HOST_USER', 'EMAIL_HOST_PASSWORD']
    for var in required_vars:
        if not os.getenv(var):
            raise ValueError(f"Error: Missing required environment variable '{var}' in .env file.")
    return config

def get_top_stories(api_key, section, limit):
    """Fetches top stories for a single section with a retry mechanism."""
    api_url = f"https://api.nytimes.com/svc/topstories/v2/{section}.json"
    params = {'api-key': api_key}
    max_retries, base_delay = 3, 5
    for attempt in range(max_retries):
        try:
            response = requests.get(api_url, params=params)
            response.raise_for_status()
            data = response.json()
            if data.get('status') == 'OK' and data.get('results'):
                return data['results'][:limit]
            else:
                return []
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                if attempt < max_retries - 1:
                    wait_time = base_delay * (2 ** attempt)
                    print(f"Warning: Rate limit hit for '{section}'. Waiting {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    print(f"Max retries reached for '{section}'. Skipping.")
                    return []
            else: raise e
        except requests.exceptions.RequestException as e:
            raise ConnectionError(f"Network error fetching '{section}': {e}")
    return []

def translate_digest_content(story_data, main_title, target_language):
    """
    Translates the main title, section titles, and all story content in one batch.
    Returns a tuple: (translated_main_title, translated_story_data)
    """
    print("Preparing all content for a single translation batch...")
    translate_client = translate.Client()
    
    # Structure to hold all text: [main_title, sec1_title, sec2_title, ..., story1_title, story1_abstract, ...]
    texts_to_translate = [main_title]
    section_keys = list(story_data.keys())
    texts_to_translate.extend([key.replace('_', ' ').replace('/', ' & ').title() for key in section_keys])

    for section in section_keys:
        for story in story_data[section]:
            texts_to_translate.append(story.get('title', ''))
            texts_to_translate.append(story.get('abstract', ''))
    
    if not texts_to_translate:
        return "", {}
    
    print(f"Translating {len(texts_to_translate)} text elements...")
    results = translate_client.translate(texts_to_translate, target_language=target_language)
    print("Translation complete.")
    
    # Reassemble the translated data
    translated_main_title = results.pop(0)['translatedText']
    
    translated_data = {}
    story_result_index = 0
    for i, original_section_key in enumerate(section_keys):
        translated_section_title = results[i]['translatedText']
        
        translated_stories_list = []
        for original_story in story_data[original_section_key]:
            # The story titles/abstracts start after all the section titles
            story_title_index = len(section_keys) + story_result_index
            story_abstract_index = len(section_keys) + story_result_index + 1
            
            translated_story = original_story.copy()
            translated_story['title'] = results[story_title_index]['translatedText']
            translated_story['abstract'] = results[story_abstract_index]['translatedText']
            translated_stories_list.append(translated_story)
            story_result_index += 2
            
        translated_data[translated_section_title] = translated_stories_list
        
    return translated_main_title, translated_data

def format_date_for_locale(locale_str):
    """Formats the current date using the specified locale."""
    try:
        # Set the locale for time formatting. Requires the locale to be installed on the system.
        locale.setlocale(locale.LC_TIME, locale_str)
        # Format: "August 13, 2025" -> "серпня 13, 2025"
        return datetime.now().strftime("%B %d, %Y")
    except locale.Error:
        print(f"Warning: Locale '{locale_str}' not found on this system. Falling back to default date format.")
        return datetime.now().strftime("%Y-%m-%d")
    finally:
        # Reset locale to default to avoid side effects
        locale.setlocale(locale.LC_TIME, '')

def format_email_body(template_path, translated_title, formatted_date, structured_stories, config):
    """Formats the HTML email body with fully translated content."""
    with open(template_path, 'r', encoding='utf-8') as f:
        template = f.read()

    stories_html = ""
    for section_title, stories in structured_stories.items():
        stories_html += f"<h2>{section_title}</h2>" # Use the already-translated section title
        for story in stories:
            stories_html += f"""
            <div class="story">
                <div class="story-title"><a href="{story['url']}">{story['title']}</a></div>
                <div class="story-abstract">{story['abstract']}</div>
                <div class="story-byline">{story['byline']}</div>
            </div>
            """
    
    return template.format(
        briefing_title=translated_title,
        date=formatted_date,
        stories_html=stories_html,
        language_name=config['target_language_name']
    )

def send_email(subject, body, recipients):
    """Sends the HTML email to a list of recipients."""
    # This function remains unchanged
    sender_email = os.getenv('EMAIL_HOST_USER')
    password = os.getenv('EMAIL_HOST_PASSWORD')
    print(f"Preparing to send email to {len(recipients)} recipient(s)...")
    smtp_server = "smtp.gmail.com"
    port = 587
    server = smtplib.SMTP(smtp_server, port)
    try:
        server.starttls()
        server.login(sender_email, password)
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = sender_email
        message["To"] = ", ".join(recipients)
        message.attach(MIMEText(body, "html"))
        server.sendmail(sender_email, recipients, message.as_string())
        print("Email sent successfully to all recipients.")
    except Exception as e:
        print(f"Error sending email: {e}")
    finally:
        server.quit()

def main():
    """Main function to run the entire process."""
    parser = argparse.ArgumentParser(description="Fetches, translates, and emails a digest of NYT Top Stories.")
    parser.add_argument('--dry-run', action='store_true', help="Run without sending emails, save output to HTML file.")
    args = parser.parse_args()

    if args.dry_run:
        print("\n*** DRY RUN MODE ACTIVATED: No emails will be sent. ***\n")
    
    print(f"--- NYT Daily Digest (Translated) started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    try:
        config = load_configuration()
        api_key = os.getenv('NYT_API_KEY')
        
        all_stories_by_section = {}
        total_sections = len(config['api_sections'])
        for i, section in enumerate(config['api_sections']):
            print(f"Fetching section {i+1}/{total_sections}: '{section}'...")
            stories = get_top_stories(api_key, section, config['max_stories_per_section'])
            if stories:
                all_stories_by_section[section] = stories
            if i < total_sections - 1:
                print("Waiting 7 seconds to respect API rate limits...")
                time.sleep(7)

        if not all_stories_by_section:
            print("No stories found. Exiting.")
            return
            
        print("\nAll sections fetched successfully.")
        
        translated_title, translated_stories = translate_digest_content(
            all_stories_by_section,
            config['main_briefing_title'],
            config['target_language']
        )
        
        formatted_date = format_date_for_locale(config['email_locale'])
        
        email_body = format_email_body(
            'email_template.html',
            translated_title,
            formatted_date,
            translated_stories,
            config
        )
        
        today_str_for_subject = datetime.now().strftime("%d.%m.%Y")
        email_subject = config['email_subject_template'].format(date=today_str_for_subject)
        
        if args.dry_run:
            output_filename = 'dry_run_digest.html'
            with open(output_filename, 'w', encoding='utf-8') as f:
                f.write(email_body)
            print("\n--- DRY RUN SUMMARY ---")
            print(f"Subject: {email_subject}")
            print(f"Recipients: {', '.join(config['recipient_emails'])}")
            print(f"Email content saved to '{output_filename}'. Preview in a browser.")
        else:
            send_email(email_subject, email_body, config['recipient_emails'])

    except Exception as e:
        print(f"\nAn unexpected error occurred: {e}")
    
    print("\n--- Process finished. ---")

if __name__ == "__main__":
    main()