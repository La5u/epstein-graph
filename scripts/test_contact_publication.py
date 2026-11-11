"""Invented fixtures: ownership evidence and fail-closed public contacts."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import contact_publication as publication
import contact_resolution as resolution
from build_fulltext_contacts import fuzzy_variants, with_phone_bases, resolver
from extract_contacts import extract


class PublicationTests(unittest.TestCase):
    def test_json_coordinates_stay_numeric_and_telephone_fields_are_audited(self):
        from audit_public_contacts import contacts_in_text
        scrub=publication.text_scrubber({},key=b'fixture key')
        coordinates={'x':600.5671875,'nested':[[600.5671875,-300.1234567]]}
        raw=json.dumps(coordinates)
        self.assertEqual(scrub(raw),raw)
        self.assertEqual(list(contacts_in_text(raw,scrub)),[])
        mixed={**coordinates,'phone':2125550147,'office_phone':'2125550148',
               'metadata':json.dumps({'x':600.5671875,'email':'private@gmail.com'})}
        clean=json.loads(scrub(json.dumps(mixed)))
        self.assertEqual(clean['x'],coordinates['x'])
        self.assertRegex(clean['phone'],r'^phone:')
        self.assertEqual(json.loads(clean['metadata'])['x'],coordinates['x'])
        leaks=list(contacts_in_text(json.dumps(mixed),scrub))
        self.assertTrue(any('2125550147' in value for value in leaks))
        self.assertTrue(any('2125550148' in value for value in leaks))
        self.assertTrue(any('private@gmail.com' in value for value in leaks))
        self.assertEqual(list(contacts_in_text(json.dumps(clean),scrub)),[])

    def test_a_person_merge_alone_cannot_publish_staff_or_ocr_mailboxes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with sqlite3.connect(root/'contact_graph.db') as db:
                db.executescript('CREATE TABLE resolutions(member,canonical,rule,organisation);'
                                 'CREATE TABLE person_contact_evidence(member,owner,rule);')
                for identity in ('email_owner','email_staff','email_ocr'):
                    db.execute('INSERT INTO resolutions VALUES (?,?,?,NULL)',(identity,'person','header_identity'))
                db.execute("INSERT INTO person_contact_evidence VALUES ('email_owner','person','direct_header_name')")
                db.execute("UPDATE resolutions SET canonical='private' WHERE member='email_staff'")
                db.execute("INSERT INTO person_contact_evidence VALUES ('email_staff','private','direct_header_name')")
                db.execute("INSERT INTO resolutions VALUES ('email_relay','org','organisation_domain','org')")
            db.close()
            public,counts=publication.publish([],{'email_owner':'owner@gmail.com','email_staff':'assistant@gmail.com',
                                                  'email_ocr':'owner2@gmail.com',
                                                  'email_relay':'relay-1234567@sale.craigslist.org'},named_ids={'person','email_staff','email_ocr'},
                                              out=root,private=root)
            self.assertEqual(public['email_owner'],'owner@gmail.com')
            self.assertRegex(public['email_staff'],r'^email:')
            self.assertRegex(public['email_ocr'],r'^email:')
            self.assertRegex(public['email_relay'],r'^email:')
            self.assertEqual(counts['plaintext_named_public_figure'],1)

    def test_classes_switch_key_stability_and_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'config').mkdir(); out=root/'output'; out.mkdir()
            with sqlite3.connect(out/'contact_graph.db') as db:
                db.execute('CREATE TABLE resolutions(member,canonical,rule,organisation)')
                db.execute('CREATE TABLE person_contact_evidence(member,owner,rule)')
                db.execute("INSERT INTO person_contact_evidence VALUES ('email_public','person','direct_header_name')")
                db.executemany('INSERT INTO resolutions VALUES (?,?,?,?)',[
                    ('email_org','org','organisation_domain','org'),
                    ('phone_org','org','organisation_switchboard','org'),
                    ('email_public','person','header_identity',None),
                    ('email_private','private','header_identity',None),
                    ('email_survivor','org','organisation_domain','org')])
            db.close()
            values={'email_org':'info@firm.test','phone_org':'+12127502408','email_public':'public@gmail.com',
                    'email_private':'private@gmail.com','email_unknown':'unknown@gmail.com','email_survivor':'protected@firm.test'}
            with patch.object(publication,'ROOT',root):
                kwargs=dict(named_ids={'person'},denied={'email_survivor'},out=out,private=root/'private')
                public,counts=publication.publish([],values,**kwargs)
                self.assertEqual(counts,dict(plaintext_org=2,plaintext_named_public_figure=1,hashed=3))
                self.assertEqual(public['email_public'],'public@gmail.com')
                self.assertEqual(public['email_org'],'info@firm.test')
                self.assertRegex(public['email_private'],r'^email:[a-f0-9]{12}$')
                self.assertEqual(public,publication.publish([],values,**kwargs)[0])
                key=publication.publication_key(root/'private')
                self.assertEqual((root/'private/public_contact_hmac.key').stat().st_mode&0o777,0o600)
                scrub=publication.text_scrubber(public,key=key)
                text=scrub({'private@gmail.com':'owner','tooltip':'Contact private@gmail.com or info@firm.test', 'search':['unknown@gmail.com','(212) 750-2409','private©gmail.com','private (a) gmail . com']})
                self.assertNotIn('private@gmail.com',json.dumps(text));self.assertNotIn('unknown@gmail.com',json.dumps(text))
                self.assertIn('info@firm.test',json.dumps(text));self.assertNotIn('750-2409',json.dumps(text))
                self.assertNotIn('©gmail',json.dumps(text,ensure_ascii=False))
                self.assertNotIn('(a) gmail',json.dumps(text))
                self.assertNotIn('private',scrub(r'private\u00a9gmail.com'))
                self.assertEqual(scrub(r'TradeMark\u00ae'),r'TradeMark\u00ae')
                chained=scrub('private@firm.test©gmail.com®aol.com')
                self.assertNotIn('@',chained);self.assertNotIn('©',chained);self.assertNotIn('®',chained)
                self.assertEqual(scrub(chained),chained)
                (root/'config/public_contacts.json').write_text('{"named_public_people":false}')
                disabled,counts=publication.publish([],values,**kwargs)
                self.assertEqual(counts,dict(plaintext_org=2,plaintext_named_public_figure=0,hashed=4))
                self.assertRegex(disabled['email_public'],r'^email:')
                self.assertNotEqual(publication.hash_label('private@gmail.com','email',b'other key'),public['email_private'])

    def test_explicit_site_service_inbox_is_allowed_without_changing_name_decisions(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'config').mkdir()
            (root/'config/public_contacts.json').write_text(json.dumps({'organisational_role_contacts':{'gdpr@service.test':'Data protection requests'}}))
            with patch.object(publication,'ROOT',root):
                result,counts=publication.publish([],{'email_service':'gdpr@service.test'},out=root,private=root)
                self.assertEqual(counts['plaintext_org'],1)
                self.assertEqual(publication.text_scrubber({},key=b'test')('gdpr@service.test'),'gdpr@service.test')

    def test_missing_evidence_and_mail_transport_metadata_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            value='owner@imap.gmail.com'
            with sqlite3.connect(root/'contact_graph.db') as db:
                db.execute('CREATE TABLE resolutions(member,canonical,rule,organisation)')
                db.execute("INSERT INTO resolutions VALUES ('email_owner','person','header_identity',NULL)")
            db.close()
            args=dict(named_ids={'person'},out=root,private=root)
            self.assertRegex(publication.publish([],{'email_owner':'owner@gmail.com'},**args)[0]['email_owner'],r'^email:')
            with sqlite3.connect(root/'contact_graph.db') as db:
                db.execute('CREATE TABLE person_contact_evidence(member,owner,rule)')
                db.execute("INSERT INTO person_contact_evidence VALUES ('email_owner','person','direct_header_name')")
            db.close()
            self.assertRegex(publication.publish([],{'email_owner':value},**args)[0]['email_owner'],r'^email:')
            self.assertRegex(publication.publish([],{'email_owner':'owner.office@gmail.com'},**args)[0]['email_owner'],r'^email:')

    def test_audit_inflates_public_payload_and_finds_private_tooltip(self):
        import base64
        import gzip
        from audit_public_contacts import viewer_texts, contacts_in_text
        raw = '{"tooltip":"private@gmail.com","safe":"info@firm.test"}'
        html = '<script id="detail-0">' + base64.b64encode(gzip.compress(raw.encode())).decode() + '</script>'
        scrub = publication.text_scrubber({'email_org':'info@firm.test'},key=b'fixture key')
        self.assertEqual(list(viewer_texts(html)),[raw])
        self.assertEqual(list(contacts_in_text(raw,scrub)),['private@gmail.com'])

    def test_approved_owner_cannot_publish_a_span_containing_private_contacts(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with sqlite3.connect(root/'contact_graph.db') as db:
                db.execute('CREATE TABLE resolutions(member,canonical,rule,organisation)')
                db.execute("INSERT INTO resolutions VALUES ('email_org','org','organisation_domain','org')")
            db.close()
            nodes=[{'id':'person','merged_identities':[{'id':'email_person'}]}]
            values={'email_org':'private@gmail.com public@firm.test',
                    'email_person':'Public Name <public@gmail.com> private@gmail.com'}
            result,counts=publication.publish(nodes,values,named_ids={'person'},out=root,private=root/'private')
            self.assertEqual(counts['hashed'],2)
            self.assertNotIn('private@gmail.com',json.dumps(result))

    def test_protected_value_exclusion_covers_other_namespaces_and_formats(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            values={'email_protected':'protected@firm.test','legacy_contact':'Protected@firm.test',
                    'phone_protected':'+12127502408','phone_alias':'(212) 750-2408'}
            result,counts=publication.publish([],values,named_ids={'legacy_contact','phone_alias'},
                denied={'email_protected','phone_protected'},kinds={'legacy_contact':'email'},out=root,private=root)
            self.assertEqual(counts['hashed'],4)
            self.assertEqual(result['email_protected'],result['legacy_contact'])
            self.assertEqual(result['phone_protected'],result['phone_alias'])

    def test_long_ocr_tokens_are_retained_without_address_regex_backtracking(self):
        with tempfile.TemporaryDirectory() as d:
            value='x'*100000
            scrub=publication.text_scrubber({},key=b'fixture key')
            self.assertEqual(scrub('private@gmail.com '+value),'email:'+publication.hash_label('private@gmail.com','email',b'fixture key').split(':')[1]+' '+value)
            result,counts=publication.publish([],{'email_long':value+'@firm.test'},named_ids={'email_long'},out=Path(d),private=Path(d))
            self.assertEqual(counts['hashed'],1)

    def test_missing_evidence_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            result,counts=publication.publish([],{'email_one':'private@gmail.com'},out=Path(d),private=Path(d))
            self.assertEqual(counts['hashed'],1)
            self.assertNotIn('private@gmail.com',json.dumps(result))

    def test_international_national_and_ocr_phone_metadata(self):
        scrub=publication.text_scrubber({},key=b'fixture key')
        for value in ('+353-1-5550141','+39 0481 555014','+971 4 5550142','+44 (0)1482 555014',
                      '+33\u00a0655\u00a0501\u00a0467','06 5550 0149','044-555-0146','(305)555-0145','212 555 0144',
                      'FAX2125550148','Tel:330155501499','Phone: 2125550147',r'+44\n1482 555014'):
            with self.subTest(value=value):
                clean=scrub(value)
                self.assertNotEqual(clean,value)
                self.assertEqual(scrub(clean),clean)
        for value in ('phone:123456789012','phone_123456789012abcdefabcdef','EFTA01234567','2015-09-08','01-13-2006'):
            self.assertEqual(scrub(value),value)
        self.assertEqual(resolution.phone_key('+44 (0)1482/555014'),'+441482555014')
        self.assertEqual(resolution.phone_key('+39 0481 555014'),'+390481555014')

    def test_phone_redaction_preserves_nested_json_csv_and_xml_escaping(self):
        import csv,html,io
        scrub=publication.text_scrubber({},key=b'fixture key')
        value={'phone':2125550147,'office_phone':'2125550148','note':'Fax: 330155501499'}
        self.assertRegex(scrub(value)['phone'],r'^phone:[a-f0-9]{12}$')
        self.assertRegex(scrub(value)['office_phone'],r'^phone:[a-f0-9]{12}$')
        for _ in range(3):
            raw=json.dumps(value);clean=scrub(raw)
            parsed=json.loads(clean)
            self.assertNotIn('2125550147',clean);self.assertNotIn('2125550148',clean);self.assertNotIn('330155501499',clean)
            self.assertEqual(scrub(clean),clean)
            stream=io.StringIO();csv.writer(stream).writerow([raw])
            json.loads(next(csv.reader(io.StringIO(scrub(stream.getvalue()))))[0])
            json.loads(html.unescape(scrub(html.escape(raw))))
            value={'metadata':raw}

    def test_audit_catches_international_and_bare_cued_phones(self):
        from audit_public_contacts import contacts_in_text
        scrub=publication.text_scrubber({},key=b'fixture key')
        found=list(contacts_in_text('+353-1-5550141 and Tel2125550147',scrub))
        self.assertTrue(any('+353' in v for v in found))
        self.assertTrue(any('2125550147' in v for v in found))
        self.assertEqual(list(contacts_in_text(scrub('+353-1-5550141 and Tel2125550147'),scrub)),[])


class ResolutionTests(unittest.TestCase):
    def test_header_only_broken_tlds_keep_real_domains_and_ambiguity(self):
        values={i:dict(kind='email',value=v) for i,v in [
            ('known','kelly@fca.org.uk'),('broken','kelly@fca.org.ulo'),
            ('real','kelly@fca.org.us'),('other','kelly@other.org.uk'),
            ('net','daniel@coreaccess.net'),('not','daniel@coreaccess.not'),
            ('com','daniel@coreaccess.com')]}
        result=resolution.broken_tld_variants(values,{'known':100,'broken':1,'real':2,'other':50,'net':100,'not':1,'com':100})
        self.assertEqual(result['broken'],('known','fca.org.uk'))
        self.assertEqual(result['not'],('net','coreaccess.net'))
        self.assertNotIn('real',result)
        values['ambiguous']=dict(kind='email',value='daniel@coreaccess.cog')
        values['country']=dict(kind='email',value='daniel@coreaccess.co')
        self.assertNotIn('ambiguous',resolution.broken_tld_variants(values,{'com':100,'ambiguous':1,'country':100}))

    def test_existing_entity_organisations_and_original_header_names(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'output').mkdir();(root/'config').mkdir()
            (root/'output/entity_nodes.json').write_text(json.dumps([
                dict(id='entity_uni',name='Northbridge University LLC',entity_type='organization')]))
            (root/'config/public_contacts.json').write_text(json.dumps({'organisation_domains':{
                'nbu.test':dict(canonical='entity_uni',document='D1')}}))
            with sqlite3.connect(root/'output/communication_graph.db') as headers:
                headers.execute('CREATE TABLE evidence(source_id,target_id,efta_number,relationship_type)')
                headers.executemany('INSERT INTO evidence VALUES (?,?,?,?)',[
                    *[('email_owner','participant_owner',f'D{i}','header_label_association') for i in range(100)],
                    *[('email_suffix','participant_owner',f'S{i}','header_label_association') for i in range(3)],
                    *[('email_domain_ocr','participant_owner',f'O{i}','header_label_association') for i in range(3)],
                    *[('email_unseen','participant_owner',f'D{i}','header_label_association') for i in range(3)],
                    *[('email_near','participant_owner',f'D{i}','header_label_association') for i in range(10)],
                    *[('email_scheme','participant_owner',f'D{i}','header_label_association') for i in range(10)],
                    *[('email_name_ocr','participant_owner',f'D{i}','header_label_association') for i in range(10)],
                    *[('email_firm','participant_owner',f'D{i}','header_label_association') for i in range(3)],
                    *[('email_firm_ocr','participant_owner',f'D{i}','header_label_association') for i in range(10)],
                    *[('email_tail','participant_owner',f'D{i}','header_label_association') for i in range(30)],
                    *[('email_initial_ocr','participant_lena',f'D{i}','header_label_association') for i in range(10)],
                    ('email_single','participant_owner','Single','header_label_association'),
                    ('email_staff','participant_staff','D2','header_label_association')])
            headers.close()
            db=sqlite3.connect(':memory:')
            db.executescript('CREATE TABLE nodes(id,kind,canonical); CREATE TABLE names(value,identity,documents);'
                             'CREATE TABLE mentions(id,efta,page,label,header);'
                             'CREATE TABLE pairs(a,b,shared_documents,shared_pages,specificity,sample_efta);')
            lookup={i:dict(kind='email',value=v) for i,v in [
                ('email_org','office@nbu.test'),('email_owner','owner@gmail.com'),
                ('email_legal','info@northbridgeuniversityllc.test'),
                ('email_staff','staff@gmail.com'),('email_ocr','owner2@gmail.com'),
                ('email_suffix','ner@gmail.com'),('email_single','new@gmail.com'),
                ('email_domain_ocr','owner@gmall.com'),
                ('email_confusable','0wner@gmail.com'),
                ('email_unseen','zelda.hargrave@agency.gov'),
                ('email_near','owner1@gmail.com'),('email_scheme','mailtoowner@gmail.com'),
                ('email_name_ocr','zeidahargrave@other.test'),
                ('email_firm','owner@firm.test'),('email_firm_ocr','owner@finn.test'),
                ('email_tail','er@gmail.com'),('email_initial_ocr','ihpilot@gmail.com'),
                ('email_full','zelda.hargrave@gmail.com')]}
            lookup.update(participant_owner=dict(kind='participant',value='Zelda Hargrave',occurrences=5),
                          participant_lena=dict(kind='participant',value='Lena Hargrave',occurrences=5),
                          participant_staff=dict(kind='participant',value='Jane Assistant',occurrences=5))
            db.executemany('INSERT INTO nodes VALUES (?,?,?)',[(i,'email',i) for i in lookup if i.startswith('email_')])
            entities=[dict(id='person',name='Zelda Hargrave',entity_type='person'),
                      dict(id='person_lena',name='Lena Hargrave',entity_type='person'),
                      dict(id='participant_owner',name='HeaderLabel_0123456789',entity_type='participant')]
            mapping={i:{'canonical':'person'} for i in ('email_owner','email_staff','email_ocr','email_suffix',
                'email_single','email_domain_ocr','email_confusable','email_unseen','email_near','email_scheme',
                'email_name_ocr','email_firm','email_firm_ocr','participant_owner','participant_staff')}
            mapping.update({i:dict(canonical='person') for i in ('email_tail',)})
            mapping.update({i:dict(canonical='person_lena') for i in ('email_initial_ocr','participant_lena')})
            source=sqlite3.connect(':memory:')
            source.execute('CREATE TABLE pages(doc_id,page,text)')
            source.execute("INSERT INTO pages VALUES ('D0',0,?)",('From: Zelda Hargrave <owner@gmail.com> '
                '<owner1@gmail.com> <mailtoowner@gmail.com> <zeidahargrave@other.test> '
                '<owner@firm.test> <owner@finn.test> <er@gmail.com> '
                'From: Lena Hargrave <ihpilot@gmail.com>',))
            with patch.object(resolution,'never_merge_pairs',return_value=[]), patch('corpus.connect',return_value=source):
                resolution.resolve_contacts(db,lookup,entities,mapping,{},set(),root)
            resolved=dict(db.execute('SELECT member,canonical FROM resolutions'))
            self.assertEqual(resolved['email_org'],'entity_uni')
            self.assertEqual(resolved['email_legal'],'entity_uni')
            self.assertEqual(resolved['email_full'],'person')
            evidence=dict(db.execute('SELECT member,owner FROM person_contact_evidence'))
            self.assertEqual(evidence['email_owner'],'person')
            self.assertNotIn('email_staff',evidence)
            self.assertNotIn('email_ocr',evidence)
            for identity in ('email_suffix','email_single','email_domain_ocr','email_confusable','email_unseen',
                             'email_near','email_scheme','email_name_ocr','email_firm','email_firm_ocr',
                             'email_tail','email_initial_ocr','email_full'):
                self.assertNotIn(identity,evidence)
            db.close()

    def test_every_requested_ocr_family_and_extension(self):
        pairs=[('john.smith@gmail.co','johnsmith@gmail.com'),('johnsmith@aol.corn','johnsmith@aol.com'),
               ('rnaittojohnsmith@gmail.com','johnsmith@gmail.com'),
               ('rnalitojohnsmith@gmail.com','johnsmith@gmail.com'),
               ('johnsmith©gmail.com','johnsmith@gmail.com'),('johnsmith(a)gmail.com','johnsmith@gmail.com'),
               (' john. smith @ gmail. com ','johnsmith@gmail.com'),('john.\nsmith@gmail.com','johnsmith@gmail.com')]
        for a,b in pairs:
            self.assertEqual(resolution.email_key(a),resolution.email_key(b))
        documents={('email','alisonmorgan@firm.com'):100,('email','a1isonrnorgan@firm.com'):1,
                   ('email','alison.morgan@firm.com'):1,('email','alis0nmorgan@firm.com'):1,
                   ('email','alisonmorgan@firrn.corn'):1}
        exact=fuzzy_variants({('email','new.user@gmail.com'):2,('email','newuser@grnail.corn'):2})
        self.assertEqual(len(exact),1)
        found=fuzzy_variants(documents)
        for kind,value in documents:
            if value!='alisonmorgan@firm.com': self.assertEqual(found[kind,value][0],'alisonmorgan@firm.com')
        self.assertEqual(resolution.phone_key('(212) 750.2408'),resolution.phone_key('+1 212 750 2408'))
        self.assertEqual(resolution.phone_key('212-750-2408 ext. 42'),'+12127502408x42')
        self.assertNotEqual(resolution.phone_key('212-750-2408 x42'),resolution.phone_key('212-750-2408 x43'))
        extracted=extract('Tel: 212-750-2408 ext. 42')
        self.assertEqual([v for k,v,*_ in extracted if k=='phone'],['+12127502408x42'])
        self.assertEqual([v for k,v,*_ in with_phone_bases(extracted)],['+12127502408x42','+12127502408'])
        self.assertEqual({v for k,v,*_ in extract('Email: jane. roe © gmail . com\nother(a)aol.corn') if k=='email'},
                         {'jane.roe@gmail.com','other@aol.corn'})

    def test_ner_email_namespace_is_not_treated_as_a_person(self):
        resolve=resolver({'email_ner':dict(kind='participant',value='new.user®gmail.com')},b'key')
        identity,rule=resolve('email','new.user@gmail.com')
        self.assertNotEqual(identity,'email_ner')
        self.assertEqual(rule,'ocr_variant:email_ner')
        self.assertEqual(resolution.email_key('newuseregmail.com'),('newuser','gmail.com'))

    def test_internal_space_repair_requires_full_known_address_on_every_page(self):
        source=sqlite3.connect(':memory:'); corpus=sqlite3.connect(':memory:')
        source.execute('CREATE TABLE mentions(kind,value,efta,page)')
        corpus.execute('CREATE TABLE pages(doc_id,page,text)')
        source.execute("INSERT INTO mentions VALUES ('email','hn@gmail.com','D1',0)")
        corpus.execute("INSERT INTO pages VALUES ('D1',0,'Write to jo hn @ gmail . com')")
        lookup={'email_known':dict(kind='email',value='john@gmail.com')}
        documents={('email','hn@gmail.com'):1}
        self.assertEqual(resolution.spaced_address_variants(source,documents,lookup,corpus),
                         {('email','hn@gmail.com'):('john@gmail.com','ocr_space_split')})
        source.execute("INSERT INTO mentions VALUES ('email','hn@gmail.com','D2',0)")
        corpus.execute("INSERT INTO pages VALUES ('D2',0,'hn@gmail.com is another correspondent')")
        self.assertEqual(resolution.spaced_address_variants(source,documents,lookup,corpus),{})
        source.close();corpus.close()

    def test_one_word_nickname_uses_the_full_name_on_its_header_contact(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'output').mkdir()
            with sqlite3.connect(root/'output/communication_graph.db') as headers:
                headers.execute('CREATE TABLE evidence(source_id,target_id,efta_number,relationship_type)')
                headers.executemany('INSERT INTO evidence VALUES (?,?,?,?)',
                    [('email_known','participant_bill','D'+str(i),'header_label_association') for i in range(3)])
            headers.close()
            db=sqlite3.connect(':memory:')
            db.executescript('CREATE TABLE nodes(id,kind,canonical);CREATE TABLE names(value,identity,documents);'
                             'CREATE TABLE mentions(id,efta,page,label,header);'
                             'CREATE TABLE pairs(a,b,shared_documents,shared_pages,specificity,sample_efta);'
                             "INSERT INTO nodes VALUES ('email_known','email','6');")
            lookup={'email_known':dict(kind='email',value='william@firm.test'),
                    'participant_bill':dict(kind='participant',value='Bill',occurrences=3)}
            with patch.object(resolution,'never_merge_pairs',return_value=[]):
                resolution.resolve_contacts(db,lookup,[dict(id=6,name='William Hargrave',entity_type='person')],{},{},set(),root)
            self.assertEqual(db.execute("SELECT canonical,rule FROM resolutions WHERE member='participant_bill'").fetchone(),
                             ('6','nickname_signature_full_name'))
            db.close()

    def test_role_mailbox_moves_without_merging_its_display_name_person(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            person = {'id': 1, 'name': 'Zelda Hargrave', 'entity_type': 'person',
                      'merged_identities': [{'id':'email_role','rule':'old_header'}]}
            org = {'id': 2, 'name': 'Acme Group', 'entity_type': 'organization'}
            (root/'graph_entities.json').write_text(json.dumps([person,org]))
            with sqlite3.connect(root/'contact_graph.db') as db:
                db.execute('CREATE TABLE resolutions(member,canonical,rule)')
                db.execute("INSERT INTO resolutions VALUES ('email_role','2','organisation_role')")
            db.close()
            nodes, edges = resolution.viewer_folds([person],[],root)
            self.assertEqual({n['id'] for n in nodes},{1,2})
            self.assertEqual(next(n for n in nodes if n['id']==1)['merged_identities'],[])
            self.assertEqual(next(n for n in nodes if n['id']==2)['merged_identities'][0]['id'],'email_role')
            self.assertEqual(len(person['merged_identities']),1)

    def test_domain_prefers_full_company_and_keeps_conflicting_surrogate_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'output').mkdir()
            db=sqlite3.connect(':memory:')
            db.executescript('CREATE TABLE nodes(id,kind,canonical);CREATE TABLE names(value,identity,documents);'
                'CREATE TABLE mentions(id,efta,page,label,header);'
                'CREATE TABLE pairs(a,b,shared_documents,shared_pages,specificity,sample_efta);')
            values={i:dict(kind='email',value=v) for i,v in [
                ('email_owner','service@acme.test'),('email_alias','alerts@acme.test'),
                ('email_shared','office@acme.test'),('email_other','info@other.test'),
                ('email_employee','zelda@acme.test')]}
            canonicals={'email_alias':'email_owner','email_other':'email_shared','email_employee':'person'}
            db.executemany('INSERT INTO nodes VALUES (?,\'email\',?)',[(i,canonicals.get(i,i)) for i in values])
            entities=[dict(id='company',name='Acme Inc.',entity_type='organization'),
                dict(id='other_company',name='Other Inc.',entity_type='organization'),
                dict(id='email_owner',name='Email_abcdef',entity_type='organization'),
                dict(id='email_shared',name='Email_123456',entity_type='organization'),
                dict(id='person',name='Zelda Hargrave',entity_type='person')]
            with patch.object(resolution,'never_merge_pairs',return_value=[]):
                resolution.resolve_contacts(db,values,entities,{},{},set(),root)
            mapping=dict(db.execute('SELECT member,canonical FROM resolutions'))
            self.assertEqual(mapping['email_owner'],'company')
            self.assertEqual(mapping['email_alias'],'company')
            self.assertEqual(mapping['email_employee'],'person')
            self.assertEqual(mapping['email_shared'],'email_shared')
            self.assertEqual(mapping['email_other'],'other_company')
            self.assertEqual(db.execute('SELECT count(*) FROM nodes').fetchone()[0],len(values))
            db.close()

    def test_merges_preserve_nodes_mentions_and_ambiguous_people(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'output').mkdir()
            db=sqlite3.connect(':memory:')
            db.executescript('CREATE TABLE nodes(id,kind,canonical); CREATE TABLE names(value,identity,documents);'
                             'CREATE TABLE mentions(id,efta,page,label,header);'
                             'CREATE TABLE pairs(a,b,shared_documents,shared_pages,specificity,sample_efta);')
            values={i:dict(kind=k,value=v) for i,k,v in [
                ('email_role','email','info@acmegroup.test'),('email_name','email','zelda.hargrave@gmail.com'),
                ('email_initial','email','zhargrave@firm.test'),('email_known','email','zh@firm.test'),
                ('phone_sig','phone','+12127502408'),('email_ambiguous','email','alex.smith@gmail.com'),
                ('email_protected','email','private.person@gmail.com'),('email_blocked','email','zelda.hargrave@aol.com')]}
            db.executemany('INSERT INTO nodes VALUES (?,?,?)',[(i,v['kind'],i) for i,v in values.items()])
            db.execute("UPDATE nodes SET canonical='1' WHERE id IN ('email_known','email_role')")
            db.execute("INSERT INTO names VALUES ('phone_sig','1',4)")
            db.executemany('INSERT INTO mentions VALUES (?,?,0,?,0)',[(i,'D'+str(n),'email') for n,i in enumerate(values)])
            entities=[dict(id=i,name=name,entity_type=kind) for i,name,kind in [
                (1,'Zelda Hargrave','person'),(2,'Acme Group LLC','organization'),
                (3,'Alex Smith','person'),(4,'Alex Smith','person'),(5,'Private Person','survivor')]]
            lookup={**values,'participant_z':dict(kind='participant',value='Zelda Hargrave',occurrences=9)}
            before=db.execute('SELECT count(*) FROM mentions').fetchone()
            with patch.object(resolution,'never_merge_pairs',return_value=[('email_blocked','kg:1')]):
                summary=resolution.resolve_contacts(db,lookup,entities,{'participant_z':{'canonical':'kg:1'},'email_role':{'canonical':'kg:1'}},{},{'private person'},root)
            mapping=dict(db.execute('SELECT member,canonical FROM resolutions'))
            self.assertEqual(mapping['email_role'],'2');self.assertEqual(mapping['email_name'],'1')
            self.assertEqual(mapping['email_initial'],'1');self.assertEqual(mapping['phone_sig'],'1')
            self.assertEqual(mapping['email_ambiguous'],'email_ambiguous')
            self.assertEqual(mapping['email_protected'],'email_protected')
            self.assertEqual(mapping['email_blocked'],'email_blocked')
            self.assertEqual(db.execute('SELECT count(*) FROM nodes').fetchone()[0],len(values))
            self.assertEqual(db.execute('SELECT count(*) FROM mentions').fetchone(),before)
            db.close()


if __name__=='__main__': unittest.main()
