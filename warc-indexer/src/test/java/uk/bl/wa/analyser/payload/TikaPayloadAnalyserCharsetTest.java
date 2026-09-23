/*
 * Added for Simple Webcrawl Manager (SWM), 2026-09-23. Part of the SWM fork
 * of netarchivesuite/warc-indexer 3.5.1; see README-SWM.md.
 */
package uk.bl.wa.analyser.payload;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;

import org.junit.Before;
import org.junit.Test;

import com.typesafe.config.ConfigFactory;

import uk.bl.wa.solr.SolrFields;
import uk.bl.wa.solr.SolrRecord;
import uk.bl.wa.solr.SolrRecordFactory;

/**
 * The charset the server declared reaches Tika, so a UTF-8 page without a
 * meta charset tag keeps its non-Latin text.
 */
public class TikaPayloadAnalyserCharsetTest {

    // No <meta charset>, lots of ASCII markup, a little Bengali and Arabic:
    // the shape Tika's byte sniffing gets wrong.
    private static final String TEXT = "তাজুল ইসলাম — قطر";
    private static final String HTML = "<!DOCTYPE html><html><head><title>Post</title>"
            + "<script>var a = 'x'.repeat(1); var b = a + a; var c = b + b;</script>"
            + "</head><body><div class=\"post\"><p>Hello there</p><p>" + TEXT + "</p>"
            + "<p>And some more ordinary ASCII text after it.</p></div></body></html>";

    private TikaPayloadAnalyser tika;

    @Before
    public void setUp() throws Exception {
        tika = new TikaPayloadAnalyser();
        tika.configure(ConfigFactory.load());
    }

    private SolrRecord record(String servedContentType) {
        SolrRecord solr = SolrRecordFactory.createFactory(null).createRecord();
        if (servedContentType != null) {
            solr.addField(SolrFields.CONTENT_TYPE_SERVED, servedContentType);
        }
        return solr;
    }

    @Test
    public void servedCharsetIsParsedInTheShapesServersUse() {
        assertEquals("UTF-8", TikaPayloadAnalyser.servedCharset(record("text/html; charset=utf-8")));
        assertEquals("UTF-8", TikaPayloadAnalyser.servedCharset(record("text/html; charset=\"utf-8\"")));
        assertEquals("UTF-8", TikaPayloadAnalyser.servedCharset(record("text/html;charset=UTF8")));
        assertEquals("windows-1256", TikaPayloadAnalyser.servedCharset(record("text/html; charset=windows-1256")));
        assertNull("no charset parameter", TikaPayloadAnalyser.servedCharset(record("text/html")));
        assertNull("unknown charset is ignored", TikaPayloadAnalyser.servedCharset(record("text/html; charset=no-such-thing")));
        assertNull("nothing served", TikaPayloadAnalyser.servedCharset(record(null)));
        assertNull("no record", TikaPayloadAnalyser.servedCharset(null));
    }

    @Test
    public void theServedCharsetKeepsNonLatinTextIntact() throws Exception {
        SolrRecord solr = record("text/html; charset=\"utf-8\"");

        tika.extract("test", solr, new ByteArrayInputStream(HTML.getBytes(StandardCharsets.UTF_8)),
                "http://example.org/post.html");

        String text = (String) solr.getField(SolrFields.SOLR_EXTRACTED_TEXT).getValue();
        assertTrue("extracted text should carry the Bengali and Arabic intact: " + text,
                text.contains(TEXT));
        assertEquals("UTF-8", solr.getFieldValue(SolrFields.CONTENT_ENCODING));
    }

    @Test
    public void aDeclaredLegacyCharsetIsHonouredToo() throws Exception {
        // The same page really sent as windows-1256 (Arabic) decodes correctly
        // only if the declared charset wins over sniffing.
        String arabic = "قطر الوطنية";
        String page = "<html><body><p>Hello</p><p>" + arabic + "</p></body></html>";
        SolrRecord solr = record("text/html; charset=windows-1256");

        tika.extract("test", solr, new ByteArrayInputStream(page.getBytes("windows-1256")),
                "http://example.org/arabic.html");

        String text = (String) solr.getField(SolrFields.SOLR_EXTRACTED_TEXT).getValue();
        assertTrue("Arabic sent as windows-1256 should decode: " + text, text.contains(arabic));
    }
}
