import os
import asyncio
import aiohttp
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Set
from datetime import datetime
from db_utils import sb
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# Configure AI
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

class RegulationScraper:
    def __init__(self, max_depth: int = 2, max_pages: int = 10):
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.visited_urls: Set[str] = set()
        self.scraped_content: List[Dict] = []
    
    async def scrape_url(self, url: str, session: aiohttp.ClientSession, depth: int = 0) -> str:
        """Scrape a single URL and return its text content"""
        if depth > self.max_depth or len(self.visited_urls) >= self.max_pages:
            return ""
        
        if url in self.visited_urls:
            return ""
        
        self.visited_urls.add(url)
        
        try:
            async with session.get(url, timeout=30) as response:
                if response.status != 200:
                    return ""
                
                html = await response.text()
                soup = BeautifulSoup(html, 'html.parser')
                
                # Remove script and style elements
                for script in soup(["script", "style", "nav", "footer", "header"]):
                    script.decompose()
                
                # Get text content
                text = soup.get_text(separator='\n', strip=True)
                
                # Clean up whitespace
                lines = (line.strip() for line in text.splitlines())
                text = '\n'.join(line for line in lines if line)
                
                print(f"✅ Scraped: {url} ({len(text)} chars)")
                
                # Find and scrape sublinks (only if within same domain)
                base_domain = urlparse(url).netloc
                links = soup.find_all('a', href=True)
                
                for link in links[:5]:  # Limit sublinks
                    href = link['href']
                    full_url = urljoin(url, href)
                    link_domain = urlparse(full_url).netloc
                    
                    # Only follow links within the same domain
                    if link_domain == base_domain and full_url not in self.visited_urls:
                        subtext = await self.scrape_url(full_url, session, depth + 1)
                        if subtext:
                            text += f"\n\n--- Content from {full_url} ---\n\n{subtext}"
                
                return text
                
        except Exception as e:
            print(f"❌ Error scraping {url}: {str(e)}")
            return ""
    
    async def scrape_regulation(self, regulation: Dict) -> Dict:
        """Scrape a regulation and all its sublinks"""
        url = regulation.get('link')
        if not url:
            return None
        
        print(f"🔍 Scraping regulation: {regulation.get('name')}")
        
        async with aiohttp.ClientSession() as session:
            content = await self.scrape_url(url, session)
        
        if not content:
            return None
        
        return {
            'regulation_id': regulation.get('id'),
            'url': url,
            'content': content,
            'scraped_at': datetime.utcnow().isoformat()
        }
    
    def analyze_with_ai(self, regulation: Dict, scraped_data: Dict) -> Dict:
        """Analyze scraped content with AI to extract key information"""
        content = scraped_data.get('content', '')
        
        if not content or len(content) < 100:
            return {
                'status': 'error',
                'status_message': 'Insufficient content scraped'
            }
        
        # Truncate content if too long (Gemini has token limits)
        max_chars = 50000
        if len(content) > max_chars:
            content = content[:max_chars] + "\n\n[Content truncated...]"
        
        prompt = f"""Analyze the following regulatory document and provide:

1. A comprehensive summary (3-5 paragraphs) of the key requirements and obligations
2. Main compliance points that businesses must follow
3. Key definitions and terms
4. Relevant penalties or consequences for non-compliance
5. Any recent updates or changes mentioned

Document Title: {regulation.get('name')}
Document URL: {regulation.get('link')}

Content:
{content}

Provide your analysis in a structured format."""

        try:
            if GEMINI_API_KEY:
                model = genai.GenerativeModel('gemini-2.0-flash-exp')
                response = model.generate_content(prompt)
                analysis = response.text
            else:
                analysis = f"AI analysis unavailable. Raw content length: {len(content)} characters."
            
            return {
                'status': 'unchanged',  # Default status, will be 'changed' if content differs
                'content': analysis,
                'raw_content': content[:5000],  # Store first 5000 chars of raw content
                'status_message': f"Successfully analyzed {len(content)} characters"
            }
        
        except Exception as e:
            print(f"❌ AI analysis error: {str(e)}")
            return {
                'status': 'error',
                'status_message': f"AI analysis failed: {str(e)}",
                'content': content[:5000]  # Fallback to raw content
            }

async def process_single_regulation(regulation: Dict) -> Dict:
    """Process a single regulation: scrape and analyze"""
    scraper = RegulationScraper(max_depth=2, max_pages=10)
    
    # Scrape content
    scraped_data = await scraper.scrape_regulation(regulation)
    
    if not scraped_data:
        return {
            'regulation_id': regulation.get('id'),
            'status': 'error',
            'status_message': 'Failed to scrape content',
            'last_checked': datetime.utcnow().isoformat()
        }
    
    # Analyze with AI
    analysis = scraper.analyze_with_ai(regulation, scraped_data)
    
    # Check if content has changed
    existing_content = regulation.get('content', '')
    new_content = analysis.get('content', '')
    
    if existing_content and existing_content != new_content:
        analysis['status'] = 'changed'
        analysis['status_message'] = 'Regulation content has been updated'
    
    return {
        'regulation_id': regulation.get('id'),
        'title': regulation.get('title') or regulation.get('name'),
        'content': new_content,
        'status': analysis.get('status'),
        'status_message': analysis.get('status_message'),
        'last_checked': datetime.utcnow().isoformat()
    }

async def process_all_regulations():
    """Process all regulations in the database"""
    print("🚀 Starting regulation scraping and analysis job...")
    
    # Get all regulations from database
    result = sb.table("regulations").select("*").execute()
    regulations = result.data if result.data else []
    
    print(f"📋 Found {len(regulations)} regulations to process")
    
    results = []
    for i, regulation in enumerate(regulations, 1):
        print(f"\n[{i}/{len(regulations)}] Processing: {regulation.get('name')}")
        
        try:
            result = await process_single_regulation(regulation)
            results.append(result)
            
            # Update database
            update_data = {
                'content': result.get('content'),
                'title': result.get('title'),
                'status': result.get('status'),
                'status_message': result.get('status_message'),
                'last_checked': result.get('last_checked'),
                'updated_at': datetime.utcnow().isoformat()
            }
            
            sb.table("regulations").update(update_data).eq("id", result['regulation_id']).execute()
            print(f"✅ Updated regulation {regulation.get('name')}: {result.get('status')}")
            
        except Exception as e:
            print(f"❌ Error processing {regulation.get('name')}: {str(e)}")
            results.append({
                'regulation_id': regulation.get('id'),
                'status': 'error',
                'status_message': str(e)
            })
        
        # Rate limiting: wait between requests
        await asyncio.sleep(2)
    
    print(f"\n✅ Completed processing {len(results)} regulations")
    return results

def run_scraper_job():
    """Synchronous wrapper to run the async scraper job"""
    return asyncio.run(process_all_regulations())

if __name__ == "__main__":
    # Run directly for testing
    results = run_scraper_job()
    print(f"\n📊 Final Results:")
    for r in results:
        print(f"  - {r['regulation_id']}: {r['status']} - {r.get('status_message', '')}")